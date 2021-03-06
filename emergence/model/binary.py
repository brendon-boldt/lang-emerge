import numpy as np
import tensorflow as tf
import tensorflow.keras
from tensorflow.keras.models import Model
from tensorflow.keras.layers import (Dense, Dropout, Input, Concatenate,
        BatchNormalization, RepeatVector, Lambda, Flatten)
from tensorflow.keras.initializers import RandomNormal
from tensorflow_probability.python.distributions import RelaxedOneHotCategorical
from numpy.random import shuffle
from numpy.linalg import matrix_rank

from .. import util

class Binary:
    """A model describing communicating an arbitrary vecotr which is first
    transformed into a random embedding."""

    @staticmethod
    def train_test_split(arr, test_split=1.0):
        """Roughly split the data ensuring that the train set has a span of the
        whole space."""
        indexes = list(range(len(arr)))
        shuffle(indexes)
        train, test = [], []
        n = arr.shape[1]
        covered = []
        for i in indexes:
            mr_covered = matrix_rank(covered)
            if sum(arr[i]) == 0.:
                continue
                # The zero vector is a degenerate case, and I do not believe it
                # is worth including
                #test.append(i)
                #train.append(i)
            if mr_covered == n:
                if len(test)/len(arr) < test_split:
                    test.append(i)
                else:
                    train.append(i)
            else:
                covered.append(arr[i])
                train.append(i)

        return train, test 

    @staticmethod
    def permutations(n):
        ma = [2**i for i in range(n)]
        arr = []
        for i in range(2**n):
            arr.append([i//b % 2 for b in ma])
        return np.array(arr)

    default_cfg = {
        # Actual batch_size == batch_size * num_concepts
        'batch_size': 7,
        'epochs': 5000,
         # How often to anneal temperature
         # More like a traditional epoch due to small dataset size
        'superepoch': 200,
        'e_dense_size': 14,
        'd_dense_size': 2,
        'input_dim': 8,
        'num_concepts': 7,
        'sentence_len': 7,
        'vocab_size': 2,

        'learning_rate': 1e-2,
        'temp_init': 3,
        'temp_decay': 0.85,
        'train_st': False,
        'test_prop': 0.1,
        'dropout_rate': 0.2,
    }

    def __init__(self, cfg=None, logdir='log'):
        if cfg is None:
            self.cfg = Binary.default_cfg
        else:
            self.cfg = {**Binary.default_cfg, **cfg} 
        self.sess = tf.Session()
        self.initialize_graph()
        self.generate_train_and_test()
        self.train_writer = tf.summary.FileWriter(
                logdir + '/train',
                self.sess.graph
                )
        self.test_writer = tf.summary.FileWriter(
                logdir + '/test',
                self.sess.graph
                )

    def gs_sampler(self, logits):
        """Sampling function for Gumbel-Softmax"""
        dist = RelaxedOneHotCategorical(temperature=self.temperature, logits=logits)
        sample = dist.sample()
        y_hard = tf.one_hot(tf.argmax(sample, -1), self.cfg['vocab_size'])
        # y_hard is the value that gets used but the gradient flows through logits
        y = tf.stop_gradient(y_hard - logits) + logits

        return tf.cond(self.straight_through, lambda: y, lambda: sample)

    def initialize_encoder(self):
        unstopped_inputs = Input(shape=(self.cfg['num_concepts'],), name='e_input')
        self.e_inputs = tf.stop_gradient(unstopped_inputs)

        # Generate a static vector space of "concepts"
        e_embeddings_w = tf.Variable(
                tf.initializers.truncated_normal(0, 1e0)(
                    (self.cfg['num_concepts'], self.cfg['input_dim'])),
                dtype=tf.float32,
                trainable=False,
                )
            
        e_x = tf.matmul(self.e_inputs, e_embeddings_w)

        # Dense layer for encocder
        e_x = Dense(self.cfg['e_dense_size'],
                activation='relu',
                name='encoder_h0')(e_x)
        #e_x = tf.layers.batch_normalization(e_x, renorm=True)
        e_x = Dense(self.cfg['vocab_size']*self.cfg['sentence_len'],
                name="encoder_word_dense")(e_x)

        self.e_raw_output = tf.keras.layers.Reshape((self.cfg['sentence_len'],
                self.cfg['vocab_size']))(e_x)

    def initialize_communication(self):
        categorical_output = self.gs_sampler

        argmax_selector = lambda: tf.one_hot(
                tf.argmax(self.e_raw_output, -1),
                self.e_raw_output.shape[-1]
                )
        gumbel_softmax_selector = lambda: Lambda(categorical_output)(self.e_raw_output)
        self.utterance = tf.cond(self.use_argmax,
                argmax_selector,
                gumbel_softmax_selector,
                name="argmax_cond",
                )

        dropout_lambda = Lambda(
                lambda x: tf.layers.dropout(
                    x,
                    noise_shape=(tf.shape(self.utterance)[0], self.cfg['sentence_len'], 1),
                    rate=self.dropout_rate,
                    training=tf.logical_not(self.use_argmax),
                    ),
                name="dropout_lambda",
                )
        self.utt_dropout = dropout_lambda(self.utterance)

    def initialize_decoder(self):
        weight_shape = (
                1,
                self.cfg['vocab_size'],
                self.cfg['d_dense_size'],
                )
        bias_shape = (
                self.cfg['sentence_len'],
                1, # Extra dim used below
                self.cfg['d_dense_size'],
                )
        d_fc_w = tf.Variable(
                tf.initializers.truncated_normal(
                    0., 1e-2)(tf.constant(weight_shape)),
                dtype=tf.float32,
                expected_shape=weight_shape,
                )
        d_fc_b = tf.Variable(
                tf.constant(1e-1, shape=bias_shape),
                dtype=tf.float32,
                expected_shape=bias_shape,
                )

        batch_size = tf.shape(self.utt_dropout)[0]

        tiled = tf.reshape(
                tf.tile(d_fc_w, (batch_size, self.cfg['sentence_len'], 1)),
                (batch_size,) + (self.cfg['sentence_len'],) + weight_shape[1:],
                )
        utt_dropout_reshaped = tf.reshape(
                self.utt_dropout,
                (-1, self.cfg['sentence_len'], 1, self.cfg['vocab_size']))

        d_x = tf.nn.relu(tf.matmul(utt_dropout_reshaped, tiled) + d_fc_b)
        d_x = Flatten(name='decoder_flatten')(d_x)

        d_x = tf.layers.batch_normalization(d_x, renorm=True)

        d_x = Dense(self.cfg['input_dim'], activation=None,
                name='decoder_output')(d_x)
        self.d_output = Dense(self.cfg['num_concepts'],
                name="decoder_class",
                activation=None,)(d_x)
        self.d_sigmoid = tf.nn.sigmoid(self.d_output)

    def initialize_graph(self):
        with tf.name_scope("hyperparameters"):
            self.dropout_rate = tf.placeholder(tf.float32, shape=(),
                    name='dropout_rate')
            self.use_argmax = tf.placeholder(tf.bool, shape=(),
                    name='use_argmax')
            self.temperature = tf.placeholder(tf.float32, shape=(),
                    name='temperature')
            self.straight_through = tf.placeholder(tf.bool, shape=(),
                    name='straight_through')

        with tf.name_scope("environment"):
            with tf.name_scope("encoder"):
                self.initialize_encoder()
            with tf.name_scope("communication"):
                self.initialize_communication()
            with tf.name_scope("decoder"):
                self.initialize_decoder()

        with tf.name_scope("training"):
            optmizier = tf.train.AdamOptimizer(self.cfg['learning_rate'])
            self.loss = tf.nn.sigmoid_cross_entropy_with_logits(
                    logits=self.d_output, labels=self.e_inputs)
            tf.summary.scalar('loss', tf.reduce_mean(self.loss))

            self.train_step = optmizier.minimize(self.loss)

        self.init_op = tf.initializers.global_variables()
        self.sess.run(self.init_op)
        self.summary = tf.summary.merge_all()

    def generate_train_and_test(self):
        all_input = Binary.permutations(self.cfg['num_concepts'])
        shuffle(all_input)
        train_i, test_i = Binary.train_test_split(
                all_input,
                self.cfg['test_prop']
                )
        train_i = np.repeat(train_i, self.cfg['batch_size'], axis=0)
        shuffle(train_i)

        # Inputs and labels are the same, so duplciate them
        train_data = all_input[train_i]
        test_data = all_input[test_i]

        self.train_fd = {
            self.e_inputs.name: train_data,
            self.temperature.name: self.cfg['temp_init'],
            self.straight_through.name: self.cfg['train_st'],
            self.dropout_rate.name: self.cfg['dropout_rate'],
            self.use_argmax.name: False,
        }
        self.test_fd = {
            self.e_inputs.name: test_data,
            self.temperature.name: self.cfg['temp_init'], # Unused
            self.straight_through.name: True, # Unused
            self.dropout_rate.name: 0., # Unused
            self.use_argmax.name: True,
        }
        #return train_data, test_data

    def run(self, verbose=False):
        # The labels are unused because they are the same as the input
        for i in range(self.cfg['epochs']):
            self.sess.run(self.train_step, feed_dict=self.train_fd)
            if i % self.cfg['superepoch'] == 0:
                self.train_fd[self.temperature.name] *= self.cfg['temp_decay']

                train_fd_use_argmax = {
                        **self.train_fd,
                        self.use_argmax.name: True
                        }
                superepoch = i // self.cfg['superepoch']
                summary = self.sess.run(
                        self.summary,
                        feed_dict=train_fd_use_argmax
                        )
                self.train_writer.add_summary(summary, superepoch)
                summary = self.sess.run(self.summary, feed_dict=self.test_fd)
                self.test_writer.add_summary(summary, superepoch)

                if verbose:
                    # TODO Fix this redundancy
                    loss = self.sess.run(
                            self.loss,
                            feed_dict=train_fd_use_argmax
                            )
                    print(f"superepoch {i // self.cfg['superepoch']}\t"
                          f"training loss: {loss.mean():.3f}")

    def train(self, inputs, labels=None, verbose=False):
        # The labels are unused because they are the same as the input
        train_fd = {
            self.e_inputs.name: inputs,
            self.temperature.name: self.cfg['temp_init'],
            self.straight_through.name: self.cfg['train_st'],
            self.dropout_rate.name: self.cfg['dropout_rate'],
            self.use_argmax.name: False,
        }

        for i in range(self.cfg['epochs']):
            self.sess.run(self.train_step, feed_dict=train_fd)
            if i % self.cfg['superepoch'] == 0:
                train_fd[self.temperature.name] *= self.cfg['temp_decay']
                if verbose:
                    summary = self.sess.run(self.summary, feed_dict=train_fd)
                    self.file_writer.add_summary(
                            summary,
                            i // self.cfg['superepoch']
                            )
                    # TODO Fix this redundancy
                    loss = self.sess.run(self.loss, feed_dict=train_fd)
                    print(f"superepoch {i // self.cfg['superepoch']}\t"
                          f"training loss: {loss.mean():.3f}")

    def test(self, verbose=False):
        # The labels are unused because they are the same as the input
        all_losses = self.sess.run(self.loss, feed_dict=self.test_fd)
        losses = np.apply_along_axis(np.average, -1, all_losses)
        if verbose:
            print(f"test loss\t"
                  f"avg: {np.average(losses):.3f}\t"
                  f"max: {np.max(losses):.3f}")

    def output_test_space(self, verbose=False):
        # Not yet implemented
        inputs = Binary.permutations(self.cfg['num_concepts'])
        fd = {**self.train_fd, self.e_inputs.name: inputs}
        results = self.sess.run(self.d_sigmoid, feed_dict=fd)
        utterances = self.sess.run(self.utterance, feed_dict=fd)
        for i in range(2**self.cfg['num_concepts']):
            sent = util.ohvs_to_words(utterances[i])
            print(f'{inputs[i]} -> {sent} -> {results[i]}')

if __name__ == '__main__':
    np.set_printoptions(formatter={'float': lambda x: "{0:0.2f}".format(x)})
    cfg = {
        'epochs': 1000,
        'verbose': True,
        'dropout_rate': 0.2,
        'print_all_sentences': False,
    }
    results = []
    #for i in range(3):
    try:
        while True:
            ap = Binary(cfg)
            writer = tf.summary.FileWriter('log', ap.sess.graph)
            results.append(ap.interactive_run())
            writer.close()
            exit(0)
            tf.reset_default_graph()
    except KeyboardInterrupt:
        pass

