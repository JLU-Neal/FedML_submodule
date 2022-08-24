import os
import pickle
import torch
import numpy as np
import logging
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "../../../../")))
import experiments.experiments_manager as experiments_manager



def transform_list_to_tensor(model_params_list):
    for k in model_params_list.keys():
        model_params_list[k] = torch.from_numpy(np.asarray(model_params_list[k])).float()
    return model_params_list


def transform_tensor_to_list(model_params):
    for k in model_params.keys():
        model_params[k] = model_params[k].detach().numpy().tolist()
    return model_params


def post_complete_message_to_sweep_process(args, is_server=False):
    pipe_path = "./tmp/fedml"
    os.system("mkdir ./tmp/; touch ./tmp/fedml")
    if not os.path.exists(pipe_path):
        os.mkfifo(pipe_path)
    pipe_fd = os.open(pipe_path, os.O_WRONLY)

    with os.fdopen(pipe_fd, 'w') as pipe:
        pipe.write("training is finished! \n%s\n" % (str(args)))

    if is_server:
        if os.path.exists("../../experiment_manager.pkl"):
            with open("../../experiment_manager.pkl", "rb") as f:
                em = pickle.load(f)
            logging.info("experiment_manager.pkl is loaded")
            logging.info(em)
        else:
            em = experiments_manager.ExperimentsManager()
            logging.info("experiment_manager.pkl not found, a new object is created")
        em.experiments.update({args.model+" "+args.fl_algorithm+" "+args.dataset:experiments_manager.experiment})
        logging.info(str(len(em.experiments))+" experiments are saved~~~~~~~~~~~~~~~")
        pickle.dump(em, open( "../../experiment_manager.pkl", "wb" ) )
        logging.info("experiment results is saved in ../../experiment_manager.pkl")



def weight_flatten(model_params):
    params = []
    for k, v in model_params.items():
        params.append(v.view(-1))
    params = torch.cat(params)

    return params