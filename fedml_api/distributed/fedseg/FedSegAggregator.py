import copy
import logging
import time
import torch
import wandb
import numpy as np
from torch import nn

from fedml_api.distributed.fedseg.utils import transform_list_to_tensor, EvaluationMetricsKeeper


class FedSegAggregator(object):
    def __init__(self, worker_num, device, model, args):
        self.worker_num = worker_num
        self.device = device
        self.args = args
        self.model_dict = dict()
        self.sample_num_dict = dict()
        self.flag_client_model_uploaded_dict = dict()

        for idx in range(self.worker_num):
            self.flag_client_model_uploaded_dict[idx] = False
        self.model = model

        self.train_acc_client_dict = dict()
        self.train_acc_class_client_dict = dict()
        self.train_mIoU_client_dict = dict()
        self.train_FWIoU_client_dict = dict()
        self.train_loss_client_dict = dict()

        self.test_acc_client_dict = dict()
        self.test_acc_class_client_dict = dict()
        self.test_mIoU_client_dict = dict()
        self.test_FWIoU_client_dict = dict()
        self.test_loss_client_dict = dict()

        logging.info('Initializing FedSegAggregator with workers: {0}'.format(worker_num))


    def init_model(self, model):
        model_params = model.state_dict()
        return model, model_params

    def get_global_model_params(self):
        return self.model.head.state_dict()

    def add_local_trained_result(self, index, model_params, sample_num):
        logging.info("add_model. index = %d" % index)
        self.model_dict[index] = model_params
        self.sample_num_dict[index] = sample_num
        self.flag_client_model_uploaded_dict[index] = True

    def check_whether_all_receive(self):
        for idx in range(self.worker_num):
            if not self.flag_client_model_uploaded_dict[idx]:
                return False
        for idx in range(self.worker_num):
            self.flag_client_model_uploaded_dict[idx] = False
        return True

    def aggregate(self):
        start_time = time.time()
        model_list = []
        training_num = 0

        for idx in range(self.worker_num):
            if self.args.is_mobile == 1:
                self.model_dict[idx] = transform_list_to_tensor(self.model_dict[idx])
            model_list.append((self.sample_num_dict[idx], self.model_dict[idx]))
            training_num += self.sample_num_dict[idx]

        logging.info("len of self.model_dict[idx] = " + str(len(self.model_dict)))

        # logging.info("################aggregate: %d" % len(model_list))
        (num0, averaged_params) = model_list[0]
        for k in averaged_params.keys():
            for i in range(0, len(model_list)):
                local_sample_number, local_model_params = model_list[i]
                w = local_sample_number / training_num
                if i == 0:
                    averaged_params[k] = local_model_params[k] * w
                else:
                    averaged_params[k] += local_model_params[k] * w

        # update the global model which is cached at the server side
        self.model.load_state_dict(averaged_params)

        end_time = time.time()
        logging.info("aggregate time cost: %d" % (end_time - start_time))
        return averaged_params

    def client_sampling(self, round_idx, client_num_in_total, client_num_per_round):
        if client_num_in_total == client_num_per_round:
            client_indexes = [client_index for client_index in range(client_num_in_total)]
        else:
            num_clients = min(client_num_per_round, client_num_in_total)
            np.random.seed(round_idx)  # make sure for each comparison, we are selecting the same clients each round
            client_indexes = np.random.choice(range(client_num_in_total), num_clients, replace=False)
        logging.info("client_indexes = %s" % str(client_indexes))
        return client_indexes

    def add_client_test_result(self, client_idx, train_eval_metrics:EvaluationMetricsKeeper, test_eval_metrics:EvaluationMetricsKeeper):
        logging.info("################add_client_test_result : {}".format(client_idx))
        
        # Populating Training Dictionary
        self.train_acc_client_dict[client_idx] = train_eval_metrics.acc
        self.train_acc_class_client_dict[client_idx] = train_eval_metrics.acc_class
        self.train_mIoU_client_dict[client_idx] = train_eval_metrics.mIoU
        self.train_FWIoU_client_dict[client_idx] = train_eval_metrics.FWIoU
        self.train_loss_client_dict[client_idx] = train_eval_metrics.loss

        # Populating Testing Dictionary
        self.test_acc_client_dict[client_idx] = test_eval_metrics.acc
        self.test_acc_class_client_dict[client_idx] = test_eval_metrics.acc_class
        self.test_mIoU_client_dict[client_idx] = test_eval_metrics.mIoU
        self.test_FWIoU_client_dict[client_idx] = test_eval_metrics.FWIoU
        self.test_loss_client_dict[client_idx] = test_eval_metrics.loss


    def output_global_acc_and_loss(self, round_idx):
        logging.info("################output_global_acc_and_loss : {}".format(round_idx))

        # Test on training set
        train_acc = np.array([self.train_acc_client_dict[k] for k in self.train_acc_client_dict.keys()]).mean()
        train_acc_class = np.array([self.train_acc_class_client_dict[k] for k in self.train_acc_class_client_dict.keys()]).mean()
        train_mIoU = np.array([self.train_mIoU_client_dict[k] for k in self.train_mIoU_client_dict.keys()]).mean()
        train_FWIoU = np.array([self.train_FWIoU_client_dict[k] for k in self.train_FWIoU_client_dict.keys()]).mean()
        train_loss = np.array([self.train_loss_client_dict[k] for k in self.train_loss_client_dict.keys()]).mean()

        # Train Logs
        wandb.log({"Train/Acc": train_acc, "round": round_idx})
        wandb.log({"Train/Acc_class": train_acc_class, "round": round_idx})
        wandb.log({"Train/mIoU": train_mIoU, "round": round_idx})
        wandb.log({"Train/FWIoU": train_FWIoU, "round": round_idx})
        wandb.log({"Train/Loss": train_loss, "round": round_idx})
        stats = {'training_acc': train_acc, 
                    'training_acc_class': train_acc_class,
                    'training_mIoU': train_mIoU,
                    'training_FWIoU': train_FWIoU,  
                    'training_loss': train_loss}
        logging.info(stats)

        # Test on testing set
        test_acc = np.array([self.test_acc_client_dict[k] for k in self.test_acc_client_dict.keys()]).mean()
        test_acc_class = np.array([self.test_acc_class_client_dict[k] for k in self.test_acc_class_client_dict.keys()]).mean()         
        test_mIoU = np.array([self.test_mIoU_client_dict[k] for k in self.test_mIoU_client_dict.keys()]).mean() 
        test_FWIoU = np.array([self.test_FWIoU_client_dict[k] for k in self.test_FWIoU_client_dict.keys()]).mean()
        test_loss = np.array([self.test_loss_client_dict[k] for k in self.test_loss_client_dict.keys()]).mean()

        # Test Logs
        wandb.log({"Test/Acc": test_acc, "round": round_idx})
        wandb.log({"Test/Acc_class": test_acc_class, "round": round_idx})
        wandb.log({"Test/mIoU": test_mIoU, "round": round_idx})
        wandb.log({"Test/FWIoU": test_FWIoU, "round": round_idx})
        wandb.log({"Test/Loss": test_loss, "round": round_idx})
        stats = {'testing_acc': test_acc, 
                    'testing_acc_class': test_acc_class,
                    'testing_mIoU': test_mIoU,
                    'testing_FWIoU': test_FWIoU,  
                    'testing_loss': test_loss}
        logging.info(stats)