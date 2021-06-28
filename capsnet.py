import numpy as np
from tensorflow.keras import layers, models, optimizers
from tensorflow.keras.layers import concatenate, Permute, Lambda
from tensorflow.keras import backend as K
from tensorflow.keras.utils import to_categorical
from tensorflow.keras import regularizers
from PIL import Image
import random
import scipy
from capslayer import *
import json

import os
import argparse
from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras import callbacks
#from tensorflow.keras.utils import multi_gpu_model

K.set_image_data_format('channels_last')

def Lane(laneID, n_class, lanesize, lanetype, lane_input, routings, stacked = 1):
    primarycaps = []
    output = layers.Conv2D(filters=lanesize*16, kernel_size=9, strides=1, padding='valid', activation='relu', name='conv1'+str(laneID)+'d0')(lane_input)
    primarycaps = primarycaps + [PrimaryCap(output, dim_capsule=16, n_channels=lanesize*2, kernel_size=6, strides=2, padding='valid', i = laneID)]

    for i in range(1, stacked):
       reshaped = Lambda(lambda ls : K.expand_dims(ls, axis=-1))(primarycaps[-1])
       output = layers.Conv2D(filters=lanesize*16, kernel_size=9, strides=1, padding='valid', activation='relu', name='conv1'+str(laneID)+'d'+str(i))(reshaped)
       primarycaps = primarycaps + [PrimaryCap(output, dim_capsule=16, n_channels=lanesize*2, kernel_size=6, strides=3, padding='valid', i = laneID + 1000*i)]

    if stacked == 1:
        allprimarycaps = primarycaps[0]
    else:
        allprimarycaps = Lambda(lambda ls : concatenate(ls, axis=1))(primarycaps)

    digitcaps = CapsuleLayer(num_capsule=1, dim_capsule=n_class, routings=routings, name='digitcaps'+str(laneID))(allprimarycaps)

    return digitcaps


def LaneCapsNet(input_shape, n_class, routings, num_lanes = 4, lanesize = 1, lanedepth = 1, lanetype = 1, gpus = 1):
    x = layers.Input(shape=input_shape, batch_size=args.batch_size)

    lanes = []
    for i in range(0, num_lanes):
        if (gpus != 0):
            with tf.device("/gpu:%d" % (i % gpus)):
                lanes = lanes + [Lane(i, n_class, lanesize, lanetype, x, routings, stacked = lanedepth)]
        else:
            lanes = lanes + [Lane(i, n_class, lanesize, lanetype, x, routings, stacked = lanedepth)]

    digitcaps1 = Lambda(lambda ls : K.permute_dimensions(concatenate(ls, axis=1), [0,2,1]))(lanes)

    digitcaps = layers.Dropout(args.dropout, (1, digitcaps1.get_shape()[2]))(digitcaps1)

    out_caps = Length(name='capsnet')(digitcaps)

    # Decoder network.
    y = layers.Input(shape=(n_class,))
    masked_by_y = Mask()([digitcaps1, y])  # The true label is used to mask the output of capsule layer. For training
    masked = Mask()(digitcaps1)  # Mask using the capsule with maximal length. For prediction

    
    # Shared Decoder model in training and prediction
    decoder = models.Sequential(name='decoder')
    decoder.add(layers.Dense(512, activation='relu', input_dim=num_lanes*n_class))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(np.prod(input_shape), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=input_shape, name='out_recon'))

    # Models for training and evaluation (prediction)
    train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
    eval_model = models.Model(x, [out_caps, decoder(masked)])

    # manipulate model
    noise = layers.Input(shape=(n_class, num_lanes))
    noised_digitcaps = layers.Add()([digitcaps1, noise])
    masked_noised_y = Mask()([noised_digitcaps, y])
    manipulate_model = models.Model([x, y, noise], decoder(masked_noised_y))

    return train_model, eval_model, manipulate_model

def margin_loss(y_true, y_pred):
    L = y_true * tf.square(tf.maximum(0., 0.9 - y_pred)) + \
            0.5 * (1 - y_true) * tf.square(tf.maximum(0., y_pred - 0.1))
    return tf.reduce_mean(tf.reduce_sum(L, 1))

def train(model, data, args, strategy):
    # unpacking the data
    (x_train, y_train), (x_test, y_test) = data

    # callbacks
    log = callbacks.CSVLogger(args.save_dir + '/log.csv')
    checkpoint = callbacks.ModelCheckpoint(args.save_dir + 'weights-{epoch:02d}.h5', monitor='val_capsnet_acc',
                                           save_best_only=True, save_weights_only=True, verbose=1)
    lr_decay = callbacks.LearningRateScheduler(schedule=lambda epoch: args.lr * (args.lr_decay ** epoch))

    with strategy.scope():
        model.compile(optimizer=optimizers.Adam(lr=args.lr),
                        loss=[margin_loss, 'mse'],
                        loss_weights=[1., args.lam_recon],
                        metrics={'capsnet': 'accuracy'})

    # Training without data augmentation:
    model.fit((x_train, y_train), (y_train, x_train), batch_size=args.batch_size, epochs=args.epochs,
              validation_data=((x_test, y_test), (y_test, x_test)), callbacks=[log, checkpoint, lr_decay],  workers=2, use_multiprocessing=True)

    model.save_weights(args.save_dir + '/trained_model.h5')
    print('Trained model saved to \'%s/trained_model.h5\'' % args.save_dir)

    return model

def test(model, data, args):
    x_test, y_test = data
    y_pred, x_recon = model.predict(x_test, batch_size=100)
    print('-'*30 + 'Begin: test' + '-'*30)
    print('Test acc:', np.sum(np.argmax(y_pred, 1) == np.argmax(y_test, 1))/y_test.shape[0])

    img = combine_images(np.concatenate([x_test[:50],x_recon[:50]]))
    image = img * 255
    Image.fromarray(image.astype(np.uint8)).save(args.save_dir + "/real_and_recon.png")
    print()
    print('Reconstructed images are saved to %s/real_and_recon.png' % args.save_dir)
    print('-' * 30 + 'End: test' + '-' * 30)
    plt.imshow(plt.imread(args.save_dir + "/real_and_recon.png"))
    plt.show()

def manipulate_latent(model, data, args):
    print('-'*30 + 'Begin: manipulate' + '-'*30)
    x_test, y_test = data
    index = np.argmax(y_test, 1) == args.digit
    number = np.random.randint(low=0, high=sum(index) - 1)
    x, y = x_test[index][number], y_test[index][number]
    x, y = np.expand_dims(x, 0), np.expand_dims(y, 0)
    noise = np.zeros([1, 10, 16])
    x_recons = []
    for dim in range(16):
        for r in [-0.25, -0.2, -0.15, -0.1, -0.05, 0, 0.05, 0.1, 0.15, 0.2, 0.25]:
            tmp = np.copy(noise)
            tmp[:,:,dim] = r
            x_recon = model.predict([x, y, tmp])
            x_recons.append(x_recon)

    x_recons = np.concatenate(x_recons)

    img = combine_images(x_recons, height=16)
    image = img*255
    Image.fromarray(image.astype(np.uint8)).save(args.save_dir + '/manipulate-%d.png' % args.digit)
    print('manipulated result saved to %s/manipulate-%d.png' % (args.save_dir, args.digit))
    print('-' * 30 + 'End: manipulate' + '-' * 30)

def load_cifar():
    # the data, shuffled and split between train and test sets
    #from keras.datasets import fashion_mnist
    #(x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()
    from keras.datasets import cifar100
    (x_train, y_train), (x_test, y_test) = cifar100.load_data()

    x_train = x_train.reshape(-1, 32, 32, 3).astype('float32') / 255.
    x_test = x_test.reshape(-1, 32, 32, 3).astype('float32') / 255.
    y_train = to_categorical(y_train.astype('float32'))
    y_test = to_categorical(y_test.astype('float32'))
    return (x_train, y_train), (x_test, y_test)

def load_mnist():
    # the data, shuffled and split between train and test sets
    from keras.datasets import fashion_mnist
    (x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()

    x_train = x_train.reshape(-1, 28, 28, 1).astype('float32') / 255.
    x_test = x_test.reshape(-1, 28, 28, 1).astype('float32') / 255.
    y_train = to_categorical(y_train.astype('float32'))
    y_test = to_categorical(y_test.astype('float32'))
    return (x_train, y_train), (x_test, y_test)

if __name__ == "__main__":


    parser = argparse.ArgumentParser(description="Multi-lane Capsule Network")

    parser.add_argument('--epochs', default=2, type=int)
    parser.add_argument('--batch_size', default=32, type=int)
    parser.add_argument('--lr', default=0.001, type=float,
                        help="Initial learning rate")
    parser.add_argument('--lr_decay', default=0.9, type=float,
                        help="The value multiplied by lr at each epoch. Set a larger value for larger epochs")
    parser.add_argument('--lam_recon', default=0.392, type=float,
                        help="The coefficient for the loss of decoder")
    parser.add_argument('-r', '--routings', default=3, type=int,
                        help="Number of iterations used in routing algorithm. should > 0")
    parser.add_argument('--shift_fraction', default=0.1, type=float,
                        help="Fraction of pixels to shift at most in each direction.")
    parser.add_argument('--debug', action='store_true',
                        help="Save weights by TensorBoard")
    parser.add_argument('--save_dir', default='./result')
    parser.add_argument('-t', '--testing', action='store_true',
                        help="Test the trained model on testing dataset")
    parser.add_argument('--digit', default=5, type=int,
                        help="Digit to manipulate")
    parser.add_argument('--dropout', default=0, type=float,
                        help="Percentage of lanes to be dropout per batch")
    parser.add_argument('--num_lanes', default=16, type=int,
                        help="Number of lanes")
    parser.add_argument('--lane_size', default=8, type=int,
                        help="Lane size")
    parser.add_argument('--lane_depth', default=1, type=int,
                        help="Lane depth")
    parser.add_argument('--gpus', default=0, type=int,
                        help="number of gpus to be used")
    parser.add_argument('--lane_type', default=1, type=int,
                        help="Type of the lane")
    parser.add_argument('-w', '--weights', default=None, help="The path of the saved weights. Should be specified when testing")
    parser.add_argument('--dataset', default='mnist')
    args = parser.parse_args()

    regularizers.l1_l2(l1=0.008, l2=0.008)

    (x_train, y_train), (x_test, y_test) = load_mnist() if args.dataset == "mnist" else load_cifar()

    strategy = tf.distribute.MultiWorkerMirroredStrategy(cluster_resolver=None, communication_options=None)

    tf_config = json.loads(os.environ['TF_CONFIG'])
    num_workers = len(tf_config['cluster']['worker'])
    args.batch_size = args.batch_size * num_workers

    with strategy.scope():
        model, eval_model, manipulate_model = LaneCapsNet(input_shape=x_train.shape[1:],
                                                        n_class=len(np.unique(np.argmax(y_train, 1))),
                                                        routings=args.routings,
                                                        num_lanes = args.num_lanes,
                                                        lanesize = args.lane_size,
                                                        lanedepth = args.lane_depth,
                                                        lanetype = args.lane_type,
                                                        gpus = args.gpus)

    model.summary()

#    gpu_model = multi_gpu_model(model, gpus=args.gpus)
    train(model=model, data=((x_train, y_train), (x_test, y_test)), args=args, strategy = strategy)
