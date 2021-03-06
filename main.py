import os
import shutil

import tensorflow as tf

import emergence as em


def run_binary_model():
    model_cfg = {
        'epochs': 5000,
        'batch_size': 4,
        'num_concepts': 6,
        'test_prop': 0.2,
        'e_dense_size': 20,
        'sentence_len': 6,
    }
    logdir = 'log'
    if os.path.isdir(logdir):
        shutil.rmtree(logdir)
    model = em.BinaryModel(cfg=model_cfg, logdir=logdir)
    model.run(verbose=True)
    model.test(verbose=True)
    #model.output_test_space(verbose=True)

if __name__ == '__main__':
    run_binary_model()
