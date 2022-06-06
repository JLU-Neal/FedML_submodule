import argparse
import copy
import json
import multiprocessing
import os
import platform
import re
import shutil
import signal
import stat
import subprocess

import time
import traceback
import urllib
import uuid
import zipfile
from os import listdir
from os.path import expanduser

import psutil
import requests
import yaml

from fedml.cli.server_deployment.mqtt_manager import MqttManager
from fedml.cli.server_deployment.yaml_utils import load_yaml_config

from fedml.core.mlops import MLOpsMetrics

import click
from fedml.core.mlops.mlops_configs import MLOpsConfigs

LOCAL_HOME_RUNNER_DIR_NAME = 'fedml-server'
LOCAL_RUNNER_INFO_DIR_NAME = 'runner_infos'


class FedMLServerRunner:
    def __init__(self, args, run_id=0,
                 request_json=None,
                 agent_config=None):
        self.fedml_packages_base_dir = None
        self.fedml_packages_unzip_dir = None
        self.mqtt_mgr = None
        self.running_request_json = dict()
        self.run_id = run_id
        self.client_mqtt_mgr = None
        self.edge_id = 0
        self.process = None
        self.args = args
        self.request_json = copy.deepcopy(request_json)
        self.version = args.version
        self.device_id = args.device_id
        self.cloud_region = args.cloud_region
        self.cur_dir = os.path.split(os.path.realpath(__file__))[0]
        if args.current_running_dir is not None:
            self.cur_dir = args.current_running_dir
        self.sudo_cmd = ""
        self.is_mac = False
        if platform.system() == "Darwin":
            self.is_mac = True

        self.agent_config = agent_config
        self.fedml_data_base_package_dir = os.path.join("/", "fedml", "data")
        self.fedml_data_local_package_dir = os.path.join("/", "fedml", "fedml-package", "fedml", "data")
        self.fedml_data_dir = self.fedml_data_base_package_dir
        self.fedml_config_dir = os.path.join("/", "fedml", "conf")

        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES = {"${FEDSYS.RUN_ID}": "",
                                                  "${FEDSYS.PRIVATE_LOCAL_DATA}": "",
                                                  "${FEDSYS.CLIENT_ID_LIST}": "",
                                                  "${FEDSYS.SYNTHETIC_DATA_URL}": "",
                                                  "${FEDSYS.IS_USING_LOCAL_DATA}": "",
                                                  "${FEDSYS.CLIENT_NUM}": "",
                                                  "${FEDSYS.CLIENT_INDEX}": "",
                                                  "${FEDSYS.CLIENT_OBJECT_LIST}": "",
                                                  "${FEDSYS.LOG_SERVER_URL}": ""}

        self.mlops_metrics = None
        click.echo("Current directory of client agent: " + self.cur_dir)

    @staticmethod
    def generate_yaml_doc(run_config_object, yaml_file):
        try:
            file = open(yaml_file, 'w', encoding='utf-8')
            yaml.dump(run_config_object, file)
            file.close()
        except Exception as e:
            click.echo("Generate yaml file.")

    def build_dynamic_constrain_variables(self, run_id, run_config, unzip_package_path):
        data_config = run_config["data_config"]
        server_edge_id_list = self.request_json["edgeids"]
        is_using_local_data = 0
        private_data_dir = data_config["privateLocalData"]
        synthetic_data_url = data_config["syntheticDataUrl"]
        edges = self.request_json["edges"]
        # if private_data_dir is not None \
        #         and len(str(private_data_dir).strip(' ')) > 0:
        #     is_using_local_data = 1
        if private_data_dir is None or len(str(private_data_dir).strip(' ')) <= 0:
            params_config = run_config.get("parameters", None)
            private_data_dir = os.path.join(unzip_package_path, "fedml", "data")
        if synthetic_data_url is None or len(str(synthetic_data_url)) <= 0:
            synthetic_data_url = private_data_dir

        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.RUN_ID}"] = run_id
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.PRIVATE_LOCAL_DATA}"] = private_data_dir.replace(' ', '')
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_ID_LIST}"] = str(server_edge_id_list).replace(' ', '')
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.SYNTHETIC_DATA_URL}"] = synthetic_data_url.replace(' ', '')
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.IS_USING_LOCAL_DATA}"] = str(is_using_local_data)
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_NUM}"] = len(server_edge_id_list)
        client_objects = str(json.dumps(edges))
        client_objects = client_objects.replace(" ", "").replace("\n", "").replace('"', '\\"')
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.CLIENT_OBJECT_LIST}"] = client_objects
        self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES["${FEDSYS.LOG_SERVER_URL}"] = self.agent_config["ml_ops_config"][
            "LOG_SERVER_URL"]

    def unzip_file(self, zip_file, unzip_file_path):
        result = False
        if zipfile.is_zipfile(zip_file):
            with zipfile.ZipFile(zip_file, 'r') as zipf:
                zipf.extractall(unzip_file_path)
                result = True

        return result

    def retrieve_and_unzip_package(self, package_name, package_url):
        package_file_no_extension = str(package_name).split('.')[0]
        home_dir = expanduser("~")
        local_package_path = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml_packages")
        try:
            os.makedirs(local_package_path)
        except Exception as e:
            click.echo("make dir")
        local_package_file = os.path.join(local_package_path, os.path.basename(package_url))
        if not os.path.exists(local_package_file):
            urllib.request.urlretrieve(package_url, local_package_file)
        local_package_file_no_extension = "run_" + str(self.run_id) + "_" + \
                                          str(os.path.basename(package_url)).split('.')[0]
        unzip_package_path = os.path.join(local_package_path, local_package_file_no_extension)
        self.fedml_packages_base_dir = unzip_package_path
        try:
            shutil.rmtree(os.path.join(unzip_package_path, package_file_no_extension), ignore_errors=True)
        except Exception as e:
            pass
        self.unzip_file(local_package_file, unzip_package_path)
        unzip_package_path = os.path.join(unzip_package_path, package_file_no_extension)
        return unzip_package_path

    def update_local_fedml_config(self, run_id, run_config):
        packages_config = run_config["packages_config"]

        # Copy config file from the client
        unzip_package_path = self.retrieve_and_unzip_package(packages_config["server"],
                                                             packages_config["serverUrl"])
        self.fedml_packages_unzip_dir = unzip_package_path
        fedml_local_config_file = unzip_package_path + os.path.join("/", "conf", "fedml.yaml")

        # Load the above config to memory
        config_from_container = load_yaml_config(fedml_local_config_file)
        container_entry_file_config = config_from_container["entry_config"]
        container_dynamic_args_config = config_from_container["dynamic_args"]
        entry_file = container_entry_file_config["entry_file"]
        conf_file = container_entry_file_config["conf_file"]
        full_conf_path = os.path.join(unzip_package_path, "fedml", "config", os.path.basename(conf_file))
        home_dir = expanduser("~")
        fedml_package_home_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME)

        # Dynamically build constrain variable with realtime parameters from server
        self.build_dynamic_constrain_variables(run_id, run_config, fedml_package_home_dir)

        # Update entry arguments value with constrain variable values with realtime parameters from server
        # currently we support the following constrain variables:
        # ${FEDSYS_RUN_ID}: a run id represented one entire Federated Learning flow
        # ${FEDSYS_PRIVATE_LOCAL_DATA}: private local data path in the Federated Learning client
        # ${FEDSYS_CLIENT_ID_LIST}: client list in one entire Federated Learning flow
        # ${FEDSYS_SYNTHETIC_DATA_URL}: synthetic data url from server,
        #                  if this value is not null, the client will download data from this URL to use it as
        #                  federated training data set
        # ${FEDSYS_IS_USING_LOCAL_DATA}: whether use private local data as federated training data set
        container_dynamic_args_config["data_cache_dir"] = "${FEDSYS.PRIVATE_LOCAL_DATA}"
        for constrain_variable_key, constrain_variable_value in self.FEDML_DYNAMIC_CONSTRAIN_VARIABLES.items():
            for argument_key, argument_value in container_dynamic_args_config.items():
                if argument_value is not None and str(argument_value).find(constrain_variable_key) == 0:
                    replaced_argument_value = str(argument_value).replace(constrain_variable_key,
                                                                          str(constrain_variable_value))
                    container_dynamic_args_config[argument_key] = replaced_argument_value

        # Merge all container new config sections as new config dictionary
        package_conf_object = dict()
        package_conf_object["entry_config"] = container_entry_file_config
        package_conf_object["dynamic_args"] = container_dynamic_args_config
        package_conf_object["dynamic_args"]["config_version"] = self.args.config_version
        container_dynamic_args_config["mqtt_config_path"] = os.path.join(unzip_package_path,
                                                                         "fedml", "config",
                                                                         os.path.basename(container_dynamic_args_config[
                                                                                              "mqtt_config_path"]))
        container_dynamic_args_config["s3_config_path"] = os.path.join(unzip_package_path,
                                                                       "fedml", "config",
                                                                       os.path.basename(container_dynamic_args_config[
                                                                                            "s3_config_path"]))
        log_file_dir = os.path.join(fedml_package_home_dir, "fedml", "logs")
        try:
            os.makedirs(log_file_dir)
        except Exception as e:
            pass
        package_conf_object["dynamic_args"]["log_file_dir"] = log_file_dir

        # Save new config dictionary to local file
        fedml_updated_config_file = os.path.join(unzip_package_path, "conf", "fedml.yaml")
        FedMLServerRunner.generate_yaml_doc(package_conf_object, fedml_updated_config_file)

        # Build dynamic arguments and set arguments to fedml config object
        self.build_dynamic_args(run_config, package_conf_object, unzip_package_path)

        return unzip_package_path, package_conf_object

    def build_dynamic_args(self, run_config, package_conf_object, base_dir):
        fedml_conf_file = package_conf_object["entry_config"]["conf_file"]
        print("fedml_conf_file:" + fedml_conf_file)
        fedml_conf_path = os.path.join(base_dir, "fedml", "config", os.path.basename(fedml_conf_file))
        fedml_conf_object = load_yaml_config(fedml_conf_path)

        # Replace local fedml config objects with parameters from MLOps web
        parameters_object = run_config.get("parameters", None)
        if parameters_object is not None:
            fedml_conf_object = parameters_object

        package_dynamic_args = package_conf_object["dynamic_args"]
        fedml_conf_object["comm_args"]["mqtt_config_path"] = package_dynamic_args["mqtt_config_path"]
        fedml_conf_object["comm_args"]["s3_config_path"] = package_dynamic_args["s3_config_path"]
        fedml_conf_object["common_args"]["using_mlops"] = True
        fedml_conf_object["train_args"]["run_id"] = package_dynamic_args["run_id"]
        fedml_conf_object["train_args"]["client_id_list"] = package_dynamic_args["client_id_list"]
        fedml_conf_object["train_args"]["client_num_in_total"] = int(package_dynamic_args["client_num_in_total"])
        fedml_conf_object["train_args"]["client_num_per_round"] = int(package_dynamic_args["client_num_in_total"])
        fedml_conf_object["device_args"]["worker_num"] = int(package_dynamic_args["client_num_in_total"])
        fedml_conf_object["data_args"]["data_cache_dir"] = package_dynamic_args["data_cache_dir"]
        fedml_conf_object["tracking_args"]["log_file_dir"] = package_dynamic_args["log_file_dir"]
        fedml_conf_object["tracking_args"]["log_server_url"] = package_dynamic_args["log_server_url"]
        bootstrap_script_file = fedml_conf_object["environment_args"]["bootstrap"]
        bootstrap_script_path = os.path.join(base_dir, "fedml", "config", os.path.basename(bootstrap_script_file))
        try:
            os.makedirs(package_dynamic_args["data_cache_dir"])
        except Exception as e:
            pass
        fedml_conf_object["dynamic_args"] = package_dynamic_args

        FedMLServerRunner.generate_yaml_doc(fedml_conf_object, fedml_conf_path)

        try:
            bootstrap_stat = os.stat(bootstrap_script_path)
            os.chmod(bootstrap_script_path, bootstrap_stat.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.system(bootstrap_script_path)
        except Exception as e:
            click.echo("Exception when executing bootstrap.sh: {}", traceback.format_exc())

    def build_image_unique_id(self, run_id, run_config):
        config_name = str(run_config.get("configName", "run_" + str(run_id)))
        config_creater = str(run_config.get("userId", "user_" + str(run_id)))
        image_unique_id = re.sub('[^a-zA-Z0-9_-]', '', str(config_name + "_" + config_creater))
        image_unique_id = image_unique_id.lower()
        return image_unique_id

    def run(self):
        click.echo("start_run: " + json.dumps(self.request_json))
        run_id = self.request_json["runId"]
        run_config = self.request_json["run_config"]
        data_config = run_config["data_config"]
        packages_config = run_config["packages_config"]
        self.run_id = run_id

        # set mqtt connection for client
        self.setup_client_mqtt_mgr()

        # report server running status
        self.mlops_metrics.report_server_training_status(run_id,
                                                         MqttManager.MSG_MLOPS_SERVER_STATUS_STARTING)

        # get training params
        private_local_data_dir = data_config.get("privateLocalData", "")
        is_using_local_data = 0
        # if private_local_data_dir is not None and len(str(private_local_data_dir).strip(' ')) > 0:
        #     is_using_local_data = 1

        # start a run according to the hyper-parameters
        # fedml_local_data_dir = self.cur_dir + "/fedml_data/run_" + str(run_id) + "_edge_" + str(edge_id)
        fedml_local_data_dir = os.path.join(self.cur_dir, "fedml_data")
        fedml_local_config_dir = os.path.join(self.cur_dir, "fedml_config")
        if is_using_local_data:
            fedml_local_data_dir = private_local_data_dir
        self.fedml_data_dir = self.fedml_data_local_package_dir

        # update local config with real time parameters from server and dynamically replace variables value
        unzip_package_path, fedml_config_object = self.update_local_fedml_config(run_id, run_config)

        entry_file_config = fedml_config_object["entry_config"]
        dynamic_args_config = fedml_config_object["dynamic_args"]
        entry_file = os.path.basename(entry_file_config["entry_file"])
        conf_file = entry_file_config["conf_file"]
        FedMLServerRunner.cleanup_learning_process()
        os.chdir(os.path.join(unzip_package_path, "fedml"))

        python_program = 'python'
        python_version_str = os.popen("python --version").read()
        if python_version_str.find("Python 3.") == -1:
            python_version_str = os.popen("python3 --version").read()
            if python_version_str.find("Python 3.") != -1:
                python_program = 'python3'

        process = subprocess.Popen([python_program, entry_file,
                                    '--cf', conf_file, '--rank', str(dynamic_args_config["rank"])])
        FedMLServerRunner.save_learning_process(process.pid)

        click.echo("test...")
        if self.check_server_is_ready():
            self.send_training_request_to_edges()
        else:
            self.stop_run()

    @staticmethod
    def exit_process(process):
        if process is None:
            return

        try:
            process.terminate()
            process.join()
            process = None
        except Exception as e:
            pass

    def reset_all_devices_status(self):
        edge_id_list = self.request_json["edgeids"]
        for edge_id in edge_id_list:
            self.mlops_metrics.report_client_training_status(edge_id, MqttManager.MSG_MLOPS_CLIENT_STATUS_IDLE)
            time.sleep(1)
        time.sleep(3)

    def stop_run(self):
        self.setup_client_mqtt_mgr()

        self.reset_all_devices_status()

        FedMLServerRunner.cleanup_learning_process()

        try:
            home_dir = expanduser("~")
            local_package_path = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml_packages")
            for package_file in listdir(local_package_path):
                if os.path.basename(package_file).startswith("run_" + str(self.run_id)):
                    shutil.rmtree(os.path.join(local_package_path, package_file), ignore_errors=True)
        except Exception as e:
            click.echo(traceback.format_exc())

        click.echo("Stop run successfully.")

    def send_training_request_to_edges(self):
        run_id = self.request_json["runId"]
        edge_id_list = self.request_json["edgeids"]
        click.echo("Edge ids: " + str(edge_id_list))
        for edge_id in edge_id_list:
            topic_start_train = "flserver_agent/" + str(edge_id) + "/start_train"
            click.echo("start_train: send topic " + topic_start_train + " to client...")
            self.client_mqtt_mgr.send_message(topic_start_train, json.dumps(self.request_json))

        time.sleep(2)

    def callback_start_train(self, topic, payload):
        click.echo("callback_start_train: topic = %s, payload = %s" % (topic, payload))

        # get training params
        request_json = json.loads(payload)
        run_id = request_json["runId"]
        self.run_id = run_id
        save_runner_infos(self.args.device_id + "." + self.args.os_name, 0, run_id=run_id)

        # Start cross-silo server with multi processing mode
        self.request_json = request_json
        self.running_request_json[str(run_id)] = request_json
        server_runner = FedMLServerRunner(self.args, run_id=run_id,
                                          request_json=request_json,
                                          agent_config=self.agent_config)
        server_process = multiprocessing.Process(target=server_runner.run)
        server_process.start()
        FedMLServerRunner.save_run_process(server_process.pid)
        # self.run()

    def check_server_is_ready(self):
        home_dir = expanduser("~")
        server_log_file = "{}/{}/fedml/logs/fedavg-cross-silo-run-{}-edge-0.log".format(home_dir,
                                                                                        LOCAL_HOME_RUNNER_DIR_NAME,
                                                                                        str(self.run_id))
        connected_flag = 'mqtt_s3.on_connect: server subscribes'
        server_check_count = 0
        server_started = False
        while True:
            server_check_count += 1
            if server_check_count >= 300:
                break

            if os.path.exists(server_log_file):
                log_lines = []
                with open(server_log_file) as file_handle:
                    log_lines = file_handle.readlines()
                for log_line in log_lines:
                    if connected_flag in log_line:
                        server_started = True
                        break

            if server_started:
                break
            else:
                time.sleep(3)

        if not server_started:
            click.echo("Server can not be started.")
            self.stop_run()
            return

        click.echo("server_started: "+str(server_started))
        time.sleep(2)
        return server_started

    def setup_client_mqtt_mgr(self):
        if self.client_mqtt_mgr is None:
            self.client_mqtt_mgr = MqttManager(
                self.agent_config["mqtt_config"]["BROKER_HOST"],
                self.agent_config["mqtt_config"]["BROKER_PORT"],
                self.agent_config["mqtt_config"]["MQTT_USER"],
                self.agent_config["mqtt_config"]["MQTT_PWD"],
                self.agent_config["mqtt_config"]["MQTT_KEEPALIVE"],
                "ServerAgent_Comm_Server" + str(uuid.uuid4()),
            )
            time.sleep(3)

        if self.mlops_metrics is None:
            self.mlops_metrics = MLOpsMetrics()
            self.mlops_metrics.set_messenger(self.client_mqtt_mgr)

    def send_training_stop_request_to_edges(self, edge_id_list, payload):
        for edge_id in edge_id_list:
            topic_stop_train = "flserver_agent/" + str(edge_id) + "/stop_train"
            click.echo("stop_train: send topic " + topic_stop_train)
            self.client_mqtt_mgr.send_message(topic_stop_train, payload)

    def callback_stop_train(self, topic, payload):
        click.echo("callback_stop_train: topic = %s, payload = %s" % (topic, payload))

        request_json = json.loads(payload)
        run_id = request_json["runId"]
        edge_id_list_str = request_json["edgeids"]
        edge_id_list = json.loads(edge_id_list_str)

        click.echo("Stopping run...")
        click.echo("Stop run with multiprocessing.")

        # set mqtt connection for client
        self.setup_client_mqtt_mgr()

        # Notify MLOps with the stopping message
        self.mlops_metrics.report_server_training_status(run_id,
                                                         MqttManager.MSG_MLOPS_SERVER_STATUS_STOPPING)

        self.send_training_stop_request_to_edges(edge_id_list, json.dumps(request_json))

        # Stop cross-silo server with multi processing mode
        stop_request_json = self.running_request_json.get(str(run_id), None)
        if stop_request_json is None:
            stop_request_json = request_json
        server_runner = FedMLServerRunner(self.args, run_id=run_id,
                                          request_json=stop_request_json,
                                          agent_config=self.agent_config)
        multiprocessing.Process(target=server_runner.stop_run).start()

        self.mlops_metrics.report_server_training_status(run_id, MqttManager.MSG_MLOPS_SERVER_STATUS_KILLED)
        if self.running_request_json.get(str(run_id), None) is not None:
            self.running_request_json.pop(str(run_id))

    def cleanup_client_with_finished_status(self):
        # set mqtt connection for client
        self.setup_client_mqtt_mgr()

        self.stop_run()

    @staticmethod
    def cleanup_run_process():
        try:
            home_dir = expanduser("~")
            local_pkg_data_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml", "data")
            process_id_file = os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME, "runner-sub-process.id")
            process_info = load_yaml_config(process_id_file)
            process_ids_str = process_info.get('process_id', '[]')
            process_ids = json.loads(process_ids_str)
            for process_id in process_ids:
                try:
                    process = psutil.Process(process_id)
                    for sub_process in process.children():
                        os.kill(sub_process.pid, signal.SIGTERM)

                    if process is not None:
                        os.kill(process.pid, signal.SIGTERM)
                except Exception as e:
                    pass

            yaml_object = {}
            yaml_object['process_id'] = '[]'
            FedMLServerRunner.generate_yaml_doc(yaml_object, process_id_file)
        except Exception as e:
            pass

    @staticmethod
    def save_run_process(process_id):
        try:
            home_dir = expanduser("~")
            local_pkg_data_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml", "data")
            process_id_file = os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME, "runner-sub-process.id")
            process_ids = []
            if os.path.exists(process_id_file) is True:
                yaml_object = load_yaml_config(process_id_file)
                process_ids_str = yaml_object.get('process_id', '[]')
                process_ids = json.loads(process_ids_str)
            process_ids.append(process_id)
            yaml_object = {}
            yaml_object['process_id'] = str(process_ids)
            FedMLServerRunner.generate_yaml_doc(yaml_object, process_id_file)
        except Exception as e:
            click.echo(traceback.format_exc())

    @staticmethod
    def cleanup_learning_process():
        try:
            home_dir = expanduser("~")
            local_pkg_data_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml", "data")
            process_id_file = os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME, "runner-learning-process.id")
            process_info = load_yaml_config(process_id_file)
            process_id = process_info.get('process_id', None)
            if process_id is not None:
                try:
                    process = psutil.Process(process_id)
                    for sub_process in process.children():
                        os.kill(sub_process.pid, signal.SIGTERM)

                    if process is not None:
                        os.kill(process.pid, signal.SIGTERM)
                except Exception as e:
                    pass
            yaml_object = {}
            yaml_object['process_id'] = -1
            FedMLServerRunner.generate_yaml_doc(yaml_object, process_id_file)
        except Exception as e:
            pass

    @staticmethod
    def save_learning_process(learning_id):
        try:
            home_dir = expanduser("~")
            local_pkg_data_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml", "data")
            process_id_file = os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME, "runner-learning-process.id")
            yaml_object = {}
            yaml_object['process_id'] = learning_id
            FedMLServerRunner.generate_yaml_doc(yaml_object, process_id_file)
        except Exception as e:
            pass

    def callback_runner_id_status(self, topic, payload):
        click.echo("callback_runner_id_status: topic = %s, payload = %s" % (topic, payload))

        request_json = json.loads(payload)
        run_id = request_json["run_id"]
        status = request_json["status"]
        self.run_id = run_id

        if status == MqttManager.MSG_MLOPS_SERVER_STATUS_FINISHED:
            click.echo("Received training finished message.")

            click.echo("Stopping training server.")

            # Stop cross-silo server with multi processing mode
            stop_request_json = self.running_request_json.get(str(run_id), None)
            if stop_request_json is None:
                stop_request_json = request_json
            server_runner = FedMLServerRunner(self.args, run_id=run_id,
                                              request_json=stop_request_json,
                                              agent_config=self.agent_config)
            multiprocessing.Process(target=server_runner.cleanup_client_with_finished_status).start()

            self.mlops_metrics.report_server_training_status(run_id, MqttManager.MSG_MLOPS_SERVER_STATUS_FINISHED)

    @staticmethod
    def get_device_id():
        if "nt" in os.name:
            # Windows will go this path
            device_id = subprocess.Popen("dmidecode.exe -s system-uuid".split())
        elif "posix" in os.name:
            # MacBook Pro and Linux (e.g., Ubuntu 20.04) will go this path
            device_id = hex(uuid.getnode())
        else:
            device_id = subprocess.Popen(
                "hal-get-property --udi /org/freedesktop/Hal/devices/computer --key system.hardware.uuid".split()
            )

        return device_id

    def bind_account_and_device_id(self, url, account_id, device_id, os_name):
        json_params = {"accountid": account_id, "deviceid": device_id, "type": os_name,
                       "gpu": "None", "processor": "", "network": ""}
        _, cert_path = MLOpsConfigs.get_instance(self.args).get_request_params()
        if cert_path is not None:
            requests.session().verify = cert_path
            response = requests.post(url, json=json_params, verify=True, headers={'Connection': 'close'})
        else:
            response = requests.post(url, json=json_params, headers={'Connection': 'close'})
        status_code = response.json().get("code")
        if status_code == "SUCCESS":
            edge_id = response.json().get("data").get("id")
        else:
            return 0
        return edge_id

    def fetch_configs(self):
        return MLOpsConfigs.get_instance(self.args).fetch_all_configs()

    def setup_mqtt_connection(self, service_config):
        # Setup MQTT connection
        self.mqtt_mgr = MqttManager(
            service_config["mqtt_config"]["BROKER_HOST"],
            service_config["mqtt_config"]["BROKER_PORT"],
            service_config["mqtt_config"]["MQTT_USER"],
            service_config["mqtt_config"]["MQTT_PWD"],
            service_config["mqtt_config"]["MQTT_KEEPALIVE"],
            self.edge_id,
        )

        self.mlops_metrics = MLOpsMetrics()
        self.mlops_metrics.set_messenger(self.mqtt_mgr)

        # Setup MQTT message listener for starting training
        topic_start_train = "flclient_agent/" + str(self.edge_id) + "/start_train"
        self.mqtt_mgr.add_message_listener(topic_start_train, self.callback_start_train)

        # Setup MQTT message listener for stopping training
        topic_stop_train = "flclient_agent/" + str(self.edge_id) + "/stop_train"
        self.mqtt_mgr.add_message_listener(topic_stop_train, self.callback_stop_train)

        # Setup MQTT message listener for server status switching
        topic_runner_id_status = "fl_server/mlops/id/status"
        self.mqtt_mgr.add_message_listener(topic_runner_id_status, self.callback_runner_id_status)

        # Start MQTT message loop
        self.mqtt_mgr.loop_forever()


def __login_internal(userid, version):
    # Build arguments for client runner.
    try:
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        # default arguments
        parser.add_argument("login", help="Login to MLOps platform")
        parser.add_argument('integers', metavar='N', type=int, nargs='+',
                            help='account id at MLOps platform')
        parser.add_argument("--version", "-v", type=str, default="release")
        parser.add_argument("--docker", "-d", type=str, default="false")
        args = parser.parse_args()
    except Exception as e:
        pass

    __login(args, userid, version)


def save_runner_infos(unique_device_id, edge_id, run_id=None):
    home_dir = expanduser("~")
    local_pkg_data_dir = os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME, "fedml", "data")
    try:
        os.makedirs(local_pkg_data_dir)
    except Exception as e:
        pass
    try:
        os.makedirs(os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME))
    except Exception as e:
        pass

    runner_info_file = os.path.join(local_pkg_data_dir, LOCAL_RUNNER_INFO_DIR_NAME, "runner_infos.yaml")
    running_info = dict()
    running_info["unique_device_id"] = str(unique_device_id)
    running_info["edge_id"] = str(edge_id)
    running_info["run_id"] = run_id
    FedMLServerRunner.generate_yaml_doc(running_info, runner_info_file)


def __login(args, userid, version):
    setattr(args, "account_id", userid)
    home_dir = expanduser("~")
    setattr(args, "current_running_dir", os.path.join(home_dir, LOCAL_HOME_RUNNER_DIR_NAME))

    sys_name = platform.system()
    if sys_name == "Darwin":
        sys_name = "MacOS"
    setattr(args, "os_name", sys_name)
    setattr(args, "version", version)
    setattr(args, "log_file_dir", os.path.join(args.current_running_dir, "fedml", "logs"))
    setattr(args, "device_id", FedMLServerRunner.get_device_id())
    setattr(args, "config_version", version)
    setattr(args, "cloud_region", "")
    click.echo(args)

    # Create server runner for communication with the FedML client.
    runner = FedMLServerRunner(args)

    # Fetch configs from the MLOps config server.
    service_config = dict()
    config_try_count = 0
    edge_id = 0
    while config_try_count < 5:
        try:
            mqtt_config, s3_config, mlops_config, docker_config = runner.fetch_configs()
            service_config["mqtt_config"] = mqtt_config
            service_config["s3_config"] = s3_config
            service_config["ml_ops_config"] = mlops_config
            service_config["docker_config"] = docker_config
            runner.agent_config = service_config
            break
        except Exception as e:
            config_try_count += 1
            time.sleep(3)
            continue

    if config_try_count >= 5:
        click.echo("Oops, you failed to login the FedML MLOps platform.")
        click.echo("Please check whether your network is normal!")
        return

    # Build unique device id
    if args.device_id is not None and len(str(args.device_id)) > 0:
        unique_device_id = "@" + args.device_id + "." + args.os_name

    # Bind account id to the MLOps platform.
    register_try_count = 0
    edge_id = 0
    # while register_try_count < 5:
    #     try:
    #         edge_id = runner.bind_account_and_device_id(
    #             service_config["ml_ops_config"]["EDGE_BINDING_URL"], args.account_id, unique_device_id, args.os_name
    #         )
    #         if edge_id > 0:
    #             runner.edge_id = edge_id
    #             break
    #     except Exception as e:
    #         register_try_count += 1
    #         time.sleep(3)
    #         continue
    #
    # if edge_id <= 0:
    #     click.echo("Oops, you failed to login the FedML MLOps platform.")
    #     click.echo("Please check whether your network is normal!")
    #     return
    runner.edge_id = edge_id

    # Log arguments and binding results.
    click.echo("login: unique_device_id = %s" % str(unique_device_id))
    click.echo("login: server_id = %s" % str(edge_id))
    save_runner_infos(args.device_id + "." + args.os_name, edge_id)

    click.echo("Congratulations, you have logged into the FedML MLOps platform successfully!")
    click.echo("Your server unique device id is " + str(unique_device_id))

    # Setup MQTT connection for communication with the FedML server.
    runner.setup_mqtt_connection(service_config)


def login(args):
    __login(args, args.user, args.version)


def logout():
    FedMLServerRunner.cleanup_run_process()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--type", "-t", help="Login or logout to MLOps platform")
    parser.add_argument("--user", "-u", type=str,
                        help='account id at MLOps platform')
    parser.add_argument("--version", "-v", type=str, default="release")
    args = parser.parse_args()
    click.echo(args)
    args.user = int(args.user)
    if args.type == 'login':
        login(args)
    else:
        logout(args)