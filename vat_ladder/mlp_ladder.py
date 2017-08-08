
import tensorflow as tf
import tensorflow.contrib.layers as layers
import os
import input_data
import time
from tqdm import tqdm
from src import *

def get_batch_ops(batch_size):
    join = lambda l, u: tf.concat([l, u], 0)
    split_lu = lambda x: (labeled(x), unlabeled(x))
    labeled = lambda x: x[:batch_size] if x is not None else x
    unlabeled = lambda x: x[batch_size:] if x is not None else x
    return join, split_lu, labeled, unlabeled

def preprocess(placeholder, params):
    return tf.reshape(placeholder, shape=[
        -1, params.cnn_init_size, params.cnn_init_size, params.cnn_fan[0]
    ]) if params.cnn else placeholder


class Activations(object):
    """Store statistics for each layer in the encoder/decoder structures

    Attributes
    ----------
        z, dict: pre-activation, used for reconstruction
        h, dict: activations
        m, dict: mean of each layer activations
        v, dict: variance of each layer activations

    """
    def __init__(self):
        self.z = {} # pre-activation
        self.h = {} # activation
        self.m = {} # mean
        self.v = {} # variance

# -----------------------------
# ENCODER
# -----------------------------
class Encoder(object):
    """MLP Encoder

    Arguments
    ---------
        inputs: tensor
        encoder_layers: sequence of ints
        bn: BatchNormLayers
        is_training: tensorflow bool
        noise_std: float
        start_layer: int
        batch_size: int
        update_batch_stats: bool

    Attributes
    ----------
        logits, pre-softmax output at final layer
        labeled, an Activations object with attributes z, h, m, v
        unlabeled, an Activations object


    """
    def __init__(self,
                 inputs,
                 encoder_layers,
                 bn,
                 is_training,
                 noise_std=0.0,
                 start_layer=0,
                 batch_size=100,
                 update_batch_stats=True):

        self.noise_std = noise_std
        self.labeled = Activations()
        self.unlabeled = Activations()
        join, split_lu, labeled, unlabeled = get_batch_ops(batch_size)

        ls = encoder_layers  # seq of layer sizes, len num_layers
        self.num_layers = len(encoder_layers) - 1

        # Layer 0: inputs, size 784
        l = 0
        h = inputs + self.generate_noise(inputs, l)
        self.labeled.z[l], self.unlabeled.z[l] = split_lu(h)

        for l in range(start_layer, self.num_layers + 1):
            print("Layer {}: {} -> {}".format(l, ls[l - 1], ls[l]))
            self.labeled.h[l-1], self.unlabeled.z[l-1] = split_lu(h)
            # z_pre = tf.matmul(h, self.W[l-1])
            z_pre = layers.fully_connected(h, num_outputs=ls[l])
            z_pre_l, z_pre_u = split_lu(z_pre)
            m, v = tf.nn.moments(z_pre_u, axes=[0])
            # save mean and variance of unlabeled examples for decoding
            self.unlabeled.m[l], self.unlabeled.v[l] = m, v

            # if training:
            def training_batch_norm():
                # Training batch normalization
                # batch normalization for labeled and unlabeled examples is performed separately
                # if noise_std > 0:
                if not update_batch_stats:
                    # Corrupted encoder
                    # batch normalization + noise
                    z = join(bn.batch_normalization(z_pre_l),
                             bn.batch_normalization(z_pre_u, m, v))
                    noise = self.generate_noise(z_pre, l)
                    z += noise
                else:
                    # Clean encoder
                    # batch normalization + update the average mean and variance using batch mean and variance of labeled examples
                    bn_l = bn.update_batch_normalization(z_pre_l, l) if \
                        update_batch_stats else bn.batch_normalization(z_pre_l)
                    bn_u = bn.batch_normalization(z_pre_u, m, v)
                    z = join(bn_l, bn_u)
                return z

            # else:
            def eval_batch_norm():
                # Evaluation batch normalization
                # obtain average mean and variance and use it to normalize the batch
                mean = bn.ewma.average(bn.running_mean[l - 1])
                var = bn.ewma.average(bn.running_var[l - 1])
                z = bn.batch_normalization(z_pre, mean, var)

                return z

            # perform batch normalization according to value of boolean "training" placeholder:
            z = tf.cond(is_training, training_batch_norm, eval_batch_norm)

            if l == self.num_layers:
                # return pre-softmax logits in final layer
                self.logits = bn.gamma[l - 1] * (z + bn.beta[l - 1])
                h = tf.nn.softmax(self.logits)
            else:
                # use ReLU activation in hidden layers
                h = tf.nn.relu(z + bn.beta[l - 1])

            self.labeled.z[l], self.labeled.z[l] = split_lu(z)
            self.labeled.h[l], self.unlabeled.h[l] = split_lu(h)

    def generate_noise(self, inputs, l):
        """Add noise depending on corruption parameters"""
        # start_layer = l+1
        # corrupt = self.params.corrupt
        # if corrupt == 'vatgauss':
        #     noise = generate_virtual_adversarial_perturbation(
        #         inputs, clean_logits, is_training=is_training,
        #         start_layer=start_layer) + \
        #         tf.random_normal(tf.shape(inputs)) * noise_std
        # elif corrupt == 'vat':
        #     noise = generate_virtual_adversarial_perturbation(
        #         inputs, clean_logits, is_training=is_training,
        #         start_layer=start_layer)
        # elif corrupt == 'gauss':
        #     noise = tf.random_normal(tf.shape(inputs)) * noise_std
        # else:
        #     noise = tf.zeros(tf.shape(inputs))
        # return noise
        return tf.random_normal(tf.shape(inputs)) * self.noise_std

# -----------------------------
# DECODER
# -----------------------------
class Decoder(object):
    """MLP Decoder

    Arguments
    ---------
        clean: Encoder object
        corr: Encoder object
        bn: BatchNormLayers object
        combinator: function with signature (z_c, u, size)
        encoder_layers: seq of ints
        denoising_cost: seq of floats
        batch_size: int


    Attributes
    ----------
        z_est: dict of tensors
        d_cost: seq of scalar tensors

    """

    def __init__(self, clean, corr, bn, combinator, encoder_layers,
                 denoising_cost, batch_size=100):

        # self.params = params
        ls = encoder_layers  # seq of layer sizes, len num_layers
        num_layers = len(encoder_layers) - 1
        # denoising_cost = params.rc_weights
        join, split_lu, labeled, unlabeled = get_batch_ops(batch_size)
        z_est = {}  # activation reconstruction
        d_cost = []  # denoising cost

        for l in range(num_layers, -1, -1):
            print("Layer {}: {} -> {}, denoising cost: {}".format(
                l, ls[l+1] if l+1<len(ls) else None,
                ls[l], denoising_cost[l]
            ))

            z, z_c = clean.unlabeled.z[l], corr.unlabeled.z[l]
            m, v = clean.unlabeled.m.get(l, 0), \
                   clean.unlabeled.v.get(l, 1 - 1e-10)
            # print(l)
            if l == num_layers:
                u = unlabeled(corr.logits)
            else:
                u = layers.fully_connected(z_est[l+1], num_outputs=ls[l])

            u = bn.batch_normalization(u)

            z_est[l] = combinator(z_c, u, ls[l])

            z_est_bn = (z_est[l] - m) / v
            # append the cost of this layer to d_cost
            d_cost.append((tf.reduce_mean(
                tf.reduce_sum(tf.square(z_est_bn - z), 1)) / ls[l]) *
                          denoising_cost[l])

        self.z_est = z_est
        self.d_cost = d_cost

# -----------------------------
# BATCH NORMALIZATION
# -----------------------------
class BatchNormLayers(object):
    """Batch norm class

    Arguments
    ---------
        ls: sequence of ints
        scope: str

    Attributes
    ----------
        bn_assigns: list of TF ops
        ewma: TF op
        running_var: list of tensors
        running_mean: list of tensors
        beta: list of tensors
        gamma: list of tensors


    """
    def __init__(self, ls, scope='bn'):

        # store updates to be made to average mean, variance
        self.bn_assigns = []
        # calculate the moving averages of mean and variance
        self.ewma = tf.train.ExponentialMovingAverage(decay=0.99)

        # average mean and variance of all layers
        # shift & scale
        with tf.variable_scope(scope, reuse=None):

            self.running_var = [tf.get_variable(
                'v'+str(i),
                initializer=tf.constant(1.0, shape=[l]),
                trainable=False) for i,l in enumerate(ls[1:])]
            self.running_mean = [tf.get_variable(
                'm'+str(i),
                initializer=tf.constant(0.0, shape=[l]),
                trainable=False) for i,l in enumerate(ls[1:])]

            # shift
            self.beta = [tf.get_variable(
                'beta'+str(i),
                initializer=tf.constant(0.0, shape=[l])
            ) for i,l in enumerate(ls[1:])]
            # scale
            self.gamma = [tf.get_variable(
                'gamma'+str(i),
                initializer=tf.constant(1.0, shape=[l]))
                for i,l in enumerate(ls[1:])]


    def update_batch_normalization(self, batch, l):
        """
        batch normalize + update average mean and variance of layer l
        if CNN, use channel-wise batch norm
        """
        # bn_axes = [0, 1, 2] if self.params.cnn else [0]
        bn_axes = list(range(len(batch.get_shape().as_list())-1))
        mean, var = tf.nn.moments(batch, axes=bn_axes)
        print(l, mean.get_shape().as_list(),
              self.running_mean[l-1].get_shape().as_list(),
              batch.get_shape().as_list())

        assign_mean = self.running_mean[l-1].assign(mean)
        assign_var = self.running_var[l-1].assign(var)
        self.bn_assigns.append(
            self.ewma.apply([self.running_mean[l-1], self.running_var[l-1]]))

        with tf.control_dependencies([assign_mean, assign_var]):
            return (batch - mean) / tf.sqrt(var + 1e-10)


    def batch_normalization(self, batch, mean=None, var=None):
        # bn_axes = [0, 1, 2] if self.params.cnn else [0]
        bn_axes = list(range(len(batch.get_shape().as_list())-1))
        if mean is None or var is None:
            mean, var = tf.nn.moments(batch, axes=bn_axes)

        return (batch - mean) / tf.sqrt(var + tf.constant(1e-10))

# -----------------------------
# COMBINATOR
# -----------------------------
def gauss_combinator(z_c, u, size):
    "gaussian denoising function proposed in the original paper"
    wi = lambda inits, name: tf.Variable(inits * tf.ones([size]), name=name)
    a1 = wi(0., 'a1')
    a2 = wi(1., 'a2')
    a3 = wi(0., 'a3')
    a4 = wi(0., 'a4')
    a5 = wi(0., 'a5')

    a6 = wi(0., 'a6')
    a7 = wi(1., 'a7')
    a8 = wi(0., 'a8')
    a9 = wi(0., 'a9')
    a10 = wi(0., 'a10')

    mu = a1 * tf.sigmoid(a2 * u + a3) + a4 * u + a5
    v = a6 * tf.sigmoid(a7 * u + a8) + a9 * u + a10

    z_est = (z_c - mu) * v + mu
    return z_est


def main():
    params = process_cli_params(get_cli_params())


    # Set GPU device to use
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(params.which_gpu)

    # Set seeds
    np.random.seed(params.seed)
    tf.set_random_seed(params.seed)

    print("===  Loading Data ===")
    mnist = input_data.read_data_sets("MNIST_data",
                                      n_labeled=params.num_labeled,
                                      one_hot=True)
    num_examples = mnist.train.num_examples

    starter_learning_rate = params.initial_learning_rate

    # epoch after which to begin learning rate decay
    decay_after = params.decay_start_epoch
    batch_size = params.batch_size
    num_iter = (num_examples // batch_size) * params.end_epoch  # number of loop iterations

    join, split_lu, labeled, unlabeled = get_batch_ops(batch_size)


    ls = params.cnn_fan if params.cnn else params.encoder_layers
    images_placeholder = tf.placeholder(tf.float32, shape=(None, ls[0]))
    images = preprocess(images_placeholder, params)
    targets = tf.placeholder(tf.float32)
    train_flag = tf.placeholder(tf.bool)

    bn = BatchNormLayers(ls)

    print("=== Clean Encoder ===")
    with tf.variable_scope('enc', reuse=None):
        clean = Encoder(inputs=images, encoder_layers=ls, bn=bn,
                        is_training=train_flag, noise_std=0.0, start_layer=0,
                        batch_size=params.batch_size, update_batch_stats=True)

    print("=== Corrupted Encoder === ")
    with tf.variable_scope('enc', reuse=True):
        corr = Encoder(inputs=images, encoder_layers=ls, bn=bn,
                        is_training=train_flag,
                        noise_std=params.encoder_noise_std, start_layer=0,
                        batch_size=params.batch_size, update_batch_stats=False)

    print("=== Decoder ===")
    with tf.variable_scope('dec', reuse=None):
        dec = Decoder(clean=clean, corr=corr, bn=bn,
                      combinator=gauss_combinator,
                      encoder_layers=ls, denoising_cost=params.rc_weights,
                      batch_size=params.batch_size)


    # Calculate total unsupervised cost
    u_cost = tf.add_n(dec.d_cost)
    pred = labeled(corr.logits)
    cost = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(
        labels=targets, logits=corr.logits))

    loss = cost + u_cost
    loss_list = [loss, cost, u_cost]

    # no of correct predictions
    correct_prediction = tf.equal(tf.argmax(clean.logits, 1), tf.argmax(targets, 1))

    accuracy = tf.reduce_mean(
        tf.cast(correct_prediction, "float")) * tf.constant(100.0)

    learning_rate = tf.Variable(starter_learning_rate, trainable=False)
    train_step = tf.train.AdamOptimizer(learning_rate).minimize(loss)

    # add the updates of batch normalization statistics to train_step
    bn_updates = tf.group(*bn.bn_assigns)
    with tf.control_dependencies([train_step]):
        train_step = tf.group(bn_updates)

    saver = tf.train.Saver(keep_checkpoint_every_n_hours=0.5, max_to_keep=5)

    # -----------------------------
    print("===  Starting Session ===")
    sess = tf.Session()

    i_iter = 0

    # -----------------------------
    # Resume from checkpoint
    ckpt_dir = "checkpoints/" + params.id + "/"
    ckpt = tf.train.get_checkpoint_state(
        ckpt_dir)  # get latest checkpoint (if any)
    if ckpt and ckpt.model_checkpoint_path:
        # if checkpoint exists, restore the parameters and set epoch_n and i_iter
        saver.restore(sess, ckpt.model_checkpoint_path)
        epoch_n = int(ckpt.model_checkpoint_path.split('/')[-1].split('-')[1])
        i_iter = (epoch_n + 1) * (num_examples // batch_size)
        print("Restored Epoch ", epoch_n)
    else:
        # no checkpoint exists. create checkpoints directory if it does not exist.
        if not os.path.exists(ckpt_dir):
            os.makedirs(ckpt_dir)
        init = tf.global_variables_initializer()
        sess.run(init)

    # -----------------------------
    # Write logs to appropriate directory
    log_dir = "logs/" + params.id
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    desc_file = log_dir + "/" + "description"
    with open(desc_file, 'a') as f:
        print(*order_param_settings(params), sep='\n', file=f, flush=True)
        print("Trainable parameters:", count_trainable_params(), file=f,
              flush=True)

    log_file = log_dir + "/" + "train_log"

    # -----------------------------
    print("=== Training ===")

    [init_acc, init_loss] = sess.run([accuracy, loss], feed_dict={
        images_placeholder: mnist.train.labeled_ds.images, targets:
            mnist.train.labeled_ds.labels,
        train_flag: False})
    print("Initial Train Accuracy: ", init_acc, "%")
    print("Initial Train Loss: ", init_loss)

    [init_acc] = sess.run([accuracy], feed_dict={
        images_placeholder: mnist.test.images, targets: mnist.test.labels, train_flag:
            False})
    print("Initial Test Accuracy: ", init_acc, "%")
    # print("Initial Test Loss: ", init_loss)


    start = time.time()
    for i in tqdm(range(i_iter, num_iter)):
        # for i in range(i_iter, num_iter):
        images, labels = mnist.train.next_batch(batch_size)

        _ = sess.run(
            [train_step],
            feed_dict={images_placeholder: images, targets: labels,
                       train_flag: True})

        if (i > 1) and ((i + 1) % (params.test_frequency_in_epochs * (
                    num_iter // params.end_epoch)) == 0):
            now = time.time() - start
            epoch_n = i // (num_examples // batch_size)
            if (epoch_n + 1) >= decay_after:
                # decay learning rate
                # learning_rate = starter_learning_rate * ((num_epochs - epoch_n) / (num_epochs - decay_after))
                ratio = 1.0 * (params.end_epoch - (
                epoch_n + 1))  # epoch_n + 1 because learning rate is set for next epoch
                ratio = max(0., ratio / (params.end_epoch - decay_after))
                sess.run(learning_rate.assign(starter_learning_rate * ratio))
            saver.save(sess, ckpt_dir + 'model.ckpt', epoch_n)


            with open(log_file, 'a') as train_log:
                # write test accuracy to file "train_log"
                # train_log_w = csv.writer(train_log)
                log_i = [now, epoch_n] + sess.run(
                    [accuracy],
                    feed_dict={images_placeholder: mnist.test.images,
                               targets: mnist.test.labels, train_flag: False}
                ) + sess.run(
                    loss_list,
                    feed_dict={images_placeholder: images, targets: labels,
                               train_flag:
                        True})
                # train_log_w.writerow(log_i)
                print(*log_i, sep=',', flush=True, file=train_log)

    print("Final Accuracy: ", sess.run(accuracy, feed_dict={
        images_placeholder: mnist.test.images, targets: mnist.test.labels,
        train_flag: False}),
          "%")

    sess.close()


if __name__ == '__main__':
    main()








