from __future__ import print_function

import os, time
from io import StringIO
import scipy.misc
import numpy as np
from glob import glob
from tqdm import trange
from itertools import chain
from collections import deque

from models import *

class Trainer(object):
    def __init__(self, config, data_loader):
        self.config = config
        self.data_loader = data_loader
        with tf.device("/cpu:0"):
            self.x, self.y = data_loader.get_inputs()
        print('self.x', self.x)
        print('self.y', self.y)

        self.optimizer = config.optimizer
        self.batch_size = config.batch_size
        self.hidden    = config.hidden

        self.step = tf.Variable(0, name='step', trainable=False)

        self.lr = tf.Variable(config.lr, name='lr')
        self.lr_update = tf.assign(self.lr, tf.maximum(self.lr * 0.5, config.lr_lower_boundary), name='lr_update')

        self.model_dir = config.model_dir
        print('self.model_dir: ', self.model_dir)

        self.use_gpu = config.use_gpu
        self.data_format = config.data_format

        #_, height, width, self.channel = get_conv_shape(self.data_loader, self.data_format)
        self.start_step = 0
        self.log_step = config.log_step
        self.max_step = config.max_step
        self.save_step = config.save_step
        self.lr_update_step = config.lr_update_step

        self.is_train = config.is_train
        with tf.device("/gpu:0" if self.use_gpu else "/cpu:0"):
            self.build_model()

        self.valStr = '' if config.is_train else '_val'
        self.saver = tf.train.Saver()# if self.is_train else None
        sumdir = self.model_dir + self.valStr
        self.summary_writer = tf.summary.FileWriter(sumdir)

        self.saveEverySec = 30
        sv = tf.train.Supervisor(logdir=self.model_dir,
                                is_chief=True,
                                saver=self.saver,
                                summary_op=None,
                                summary_writer=self.summary_writer,
                                save_model_secs=self.saveEverySec if self.is_train else 0,
                                global_step=self.step,
                                ready_for_local_init_op=None)

        gpu_options = tf.GPUOptions(allow_growth=True)
        sess_config = tf.ConfigProto(allow_soft_placement=True,
                                    gpu_options=gpu_options)

        self.sess = sv.prepare_or_wait_for_session(config=sess_config)
        # start our custom queue runner's threads
        if True:#self.is_train:
            self.data_loader.start_threads(self.sess)

    def train(self):
        totStep = 0
        for ep in range(1, self.config.epoch + 1):
            trainBar = trange(self.start_step, self.data_loader.NumBatchTrain)
            for step in trainBar:
                totStep += 1
                fetch_dict = {"optim": self.optim}
                if step % self.log_step == 0:
                    fetch_dict.update({
                        "summary": self.summary_op,
                        "loss": self.loss,
                        "logloss": self.logloss
                    })
                result = self.sess.run(fetch_dict)

                if step % self.log_step == 0:
                    self.summary_writer.add_summary(result['summary'], totStep)
                    self.summary_writer.flush()

                    loss = result['loss']
                    logloss = result['logloss']
                    trainBar.set_description("epoch:{:03d}, L:{:.4f}, logL:{:+.3f}, q:{:d}". \
                        format(ep, loss, logloss, self.data_loader.size_op.eval(session=self.sess)))

                if totStep % self.lr_update_step == self.lr_update_step - 1:
                    self.sess.run([self.lr_update])

    def validate(self):
        numSteps = 50#self.data_loader.NumBatchValid
        trainBar = trange(self.start_step, numSteps)
        sleepTime = (self.saveEverySec/2) / numSteps
        print('sleepTime', sleepTime)
        for step in trainBar:
            fetch_dict = {}
            if True:#step % self.log_step == 0:
                fetch_dict.update({
                    "summary": self.summary_op,
                    "loss": self.loss,
                    "logloss": self.logloss,
                    "step": self.step
                })
            result = self.sess.run(fetch_dict)

            if True:#step % self.log_step == 0:
                self.summary_writer.add_summary(result['summary'], result['step'] + step)
                self.summary_writer.flush()

                loss = result['loss']
                logloss = result['logloss']
                trainBar.set_description("q:{}, L:{:.6f}, logL:{:.6f}". \
                    format(self.data_loader.size_op.eval(session=self.sess), loss, logloss))
            time.sleep(sleepTime)

    def build_model(self):
        x = self.x
        y = self.y
        print('x:', x)
        print('y:', y)

        net = x
        nLayPrev = self.data_loader.n_input
        iLay = 0
        for nLay in self.config.hidden.split(','):
            iLay += 1
            nLay = int(nLay)
            net = nn_layer(net, nLayPrev, nLay, 'layer'+str(iLay))
            nLayPrev = nLay
        pred = nn_layer(net, nLayPrev, self.data_loader.n_output, 'layerout', act=tf.identity)
        print('pred:', pred)

        # Add ops to save and restore all the variables.
        with tf.name_scope('loss'):
            self.loss = tf.losses.mean_squared_error(y, pred)

        with tf.name_scope('logloss'):
            self.logloss = tf.log(self.loss) / tf.log(10.0) # add a tiny bias to avoid numerical error

        self.summary_op = tf.summary.merge([
            tf.summary.histogram("x", self.x),
            tf.summary.histogram("y", self.y),
            tf.summary.scalar("loss/loss", self.loss),
            tf.summary.scalar("loss/logloss", self.logloss),
        ])

        if self.is_train:
            if self.optimizer == 'adam':
                optimizer = tf.train.AdamOptimizer
            elif self.optimizer == 'sgd':
                optimizer = tf.train.GradientDescentOptimizer
            else:
                raise Exception("[!] Caution! Paper didn't use {} opimizer other than Adam".format(config.optimizer))

            optimizer = optimizer(self.lr)

            slim.losses.add_loss(self.loss)
            total_loss = slim.losses.get_total_loss()
            self.optim = train_op = slim.learning.create_train_op(total_loss, optimizer, global_step=self.step)#optimizer.minimize(self.loss)

def variable_summaries(var):
  """Attach a lot of summaries to a Tensor (for TensorBoard visualization)."""
  with tf.name_scope('summaries'):
    mean = tf.reduce_mean(var)
    tf.summary.scalar('mean', mean)
    with tf.name_scope('stddev'):
      stddev = tf.sqrt(tf.reduce_mean(tf.square(var - mean)))
    tf.summary.scalar('stddev', stddev)
    tf.summary.scalar('max', tf.reduce_max(var))
    tf.summary.scalar('min', tf.reduce_min(var))
    tf.summary.histogram('histogram', var)

def weight_variable(shape):
  initial = tf.truncated_normal(shape, stddev=1.)
  return tf.Variable(initial)

def bias_variable(shape):
  initial = tf.constant(1., shape=shape)
  return tf.Variable(initial)

def nn_layer(input_tensor, input_dim, output_dim, layer_name, act=tf.nn.sigmoid):
  # Adding a name scope ensures logical grouping of the layers in the graph.
  with tf.name_scope(layer_name):
    # This Variable will hold the state of the weights for the layer
    with tf.name_scope('weights'):
      weights = weight_variable([input_dim, output_dim])
      variable_summaries(weights)
    with tf.name_scope('biases'):
      biases = bias_variable([output_dim])
      variable_summaries(biases)
    with tf.name_scope('Wx_plus_b'):
      preactivate = tf.matmul(input_tensor, weights) + biases
      tf.summary.histogram('pre_activations', preactivate)
    activations = act(preactivate, name='activation')
    tf.summary.histogram('activations', activations)
    print('layer_name', layer_name)
    print('input_tensor', input_tensor)
    print('input_dim', input_dim, ' output_dim', output_dim)
    print('weights', weights)
    print('biases', biases)
    print('preactivate', preactivate)
    print('activations', activations)
    return activations
