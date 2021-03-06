from __future__ import division

import time
import six
from keras.models import Model
from keras.layers import (
    Input,
    Activation,
    Dense,
    Flatten,
    Permute,
    Dropout
)
from keras.layers import (
    GlobalMaxPooling2D,
    GlobalAveragePooling2D
)
from keras.layers.convolutional import (
    Conv2D,
    MaxPooling2D,
    AveragePooling2D
)
from keras.layers.merge import add
from keras.layers.normalization import BatchNormalization
from keras.regularizers import l2
from keras.constraints import max_norm
from keras import backend as K

import os
os.environ["PATH"] += os.pathsep + 'C:/Program Files (x86)/Graphviz2.38/bin/'


def _bn_relu(input):
    """Helper to build a BN -> relu block
    """
    norm = BatchNormalization(axis=CHANNEL_AXIS)(input)
    
    return Activation("relu")(norm)


def _conv_bn_relu(**conv_params):
    """Helper to build a conv -> BN -> relu block
    """
    filters = conv_params["filters"]
    kernel_size = conv_params["kernel_size"]
    strides = conv_params.setdefault("strides", (1, 1))
    kernel_initializer = conv_params.setdefault("kernel_initializer", "he_normal")
    padding = conv_params.setdefault("padding", "same")
    kernel_regularizer = conv_params.setdefault("kernel_regularizer", l2(1.e-4))

    def f(input):
        conv = Conv2D(filters=filters, kernel_size=kernel_size,
                      strides=strides, padding=padding,
                      kernel_initializer=kernel_initializer,
                      kernel_regularizer=kernel_regularizer)(input)
        return _bn_relu(conv)

    return f


def _bn_relu_conv(**conv_params):
    """Helper to build a BN -> relu -> conv block.
    This is an improved scheme proposed in http://arxiv.org/pdf/1603.05027v2.pdf
    """
    filters = conv_params["filters"]
    kernel_size = conv_params["kernel_size"]
    strides = conv_params.setdefault("strides", (1, 1))
    kernel_initializer = conv_params.setdefault("kernel_initializer", "he_normal")
    padding = conv_params.setdefault("padding", "same")
    kernel_regularizer = conv_params.setdefault("kernel_regularizer", l2(1.e-4))

    dropout_keep_prob = 0.8
    
    def f(input):
        activation = _bn_relu(input)
        activation = Dropout(1.0 - dropout_keep_prob)(activation)
        return Conv2D(filters=filters, kernel_size=kernel_size,
                      strides=strides, padding=padding,
                      kernel_initializer=kernel_initializer,
                      kernel_regularizer=kernel_regularizer)(activation)

    return f


def _shortcut(input, residual):
    input_shape = K.int_shape(input)
    residual_shape = K.int_shape(residual)
    stride_width = int(round(input_shape[ROW_AXIS] / residual_shape[ROW_AXIS]))
    stride_height = int(round(input_shape[COL_AXIS] / residual_shape[COL_AXIS]))
    equal_channels = input_shape[CHANNEL_AXIS] == residual_shape[CHANNEL_AXIS]

    shortcut = input
    
    if stride_width > 1 or stride_height > 1 or not equal_channels:
        shortcut = Conv2D(filters=residual_shape[CHANNEL_AXIS],
                          kernel_size=(1, 1),
                          strides=(stride_width, stride_height),
                          padding="valid",
                          kernel_initializer="he_normal",
                          kernel_regularizer=l2(1.e-4))(input)

    return add([shortcut, residual])


def _residual_block(block_function, filters, repetitions, is_first_layer=False):
    def f(input):
        for i in range(repetitions):
            init_strides = (1, 1)
            if i == 0 and not is_first_layer:
                init_strides = (2, 2)
            input = block_function(filters=filters, init_strides=init_strides,
                                   is_first_block_of_first_layer=(is_first_layer and i == 0))(input)
        return input

    return f


def basic_block(filters, init_strides=(1, 1), is_first_block_of_first_layer=False):
    def f(input):

        if is_first_block_of_first_layer:
            # don't repeat bn->relu since we just did bn->relu->maxpool
            conv1 = Conv2D(filters=filters, kernel_size=(3, 3),
                           strides=init_strides,
                           padding="same",
                           kernel_initializer="he_normal",
                           kernel_regularizer=l2(1.e-4))(input)
        else:
            conv1 = _bn_relu_conv(filters=filters, kernel_size=(3, 3),
                                  strides=init_strides)(input)
        
        residual = _bn_relu_conv(filters=filters, kernel_size=(3, 3))(conv1)

        return _shortcut(input, residual)

    return f


def bottleneck(filters, init_strides=(1, 1), is_first_block_of_first_layer=False):
    dropout_keep_prob = 0.8
    
    def f(input):

        if is_first_block_of_first_layer:
            # don't repeat bn->relu since we just did bn->relu->maxpool
            conv_1_1 = Conv2D(filters=filters, kernel_size=(1, 1),
                              strides=init_strides,
                              padding="same",
                              kernel_initializer="he_normal",
                              kernel_regularizer=l2(1.e-4))(input)
        else:
            conv_1_1 = _bn_relu_conv(filters=filters, kernel_size=(1, 1),
                                     strides=init_strides)(input)

        conv_3_3 = _bn_relu_conv(filters=filters, kernel_size=(3, 3))(conv_1_1)
        residual = _bn_relu_conv(filters=filters * 4, kernel_size=(1, 1))(conv_3_3)
        
        residual = Dropout(1.0 - dropout_keep_prob)(residual)

        return _shortcut(input, residual)

    return f


def _handle_dim_ordering():
    global ROW_AXIS
    global COL_AXIS
    global CHANNEL_AXIS
    if K.backend() == 'tensorflow':
        ROW_AXIS = 1
        COL_AXIS = 2
        CHANNEL_AXIS = 3
    else:
        CHANNEL_AXIS = 1
        ROW_AXIS = 2
        COL_AXIS = 3


def _get_block(identifier):
    if isinstance(identifier, six.string_types):
        res = globals().get(identifier)
        if not res:
            raise ValueError('Invalid {}'.format(identifier))
        return res
    return identifier


class ResnetBuilder(object):
    @staticmethod
    def build(input_shape, num_outputs, block_fn, repetitions):
        dropout_keep_prob = 0.8
        
        _handle_dim_ordering()
        if len(input_shape) != 3:
            raise Exception("Input shape should be a tuple (nb_channels, nb_rows, nb_cols)")

        # Permute dimension order if necessary
        ##if K.image_dim_ordering() == 'tf':
        ##    input_shape = (input_shape[1], input_shape[2], input_shape[0])

        input = Input(shape=input_shape)

        block_fn = _get_block(block_fn)
        filters_mat = [32, 48, 64, 96, 128, 192, 256, 384, 512]
        
        if input_shape == (128, 128, 1):
            # Regression: For a receptive size, kernel_size = (3, 3) is optimal.
            conv1 = _conv_bn_relu(filters=filters_mat[2], kernel_size=(3, 3), strides=(2, 2), is_first_layer = False)(input)
            conv1 = _conv_bn_relu(filters=filters_mat[2], kernel_size=(3, 3), strides=(1, 1), is_first_layer = False)(conv1)
            conv1 = _conv_bn_relu(filters=filters_mat[4], kernel_size=(3, 3), strides=(1, 1), is_first_layer = False)(conv1)
            
            # Regression: AveragePooling2D works better than MaxPooling2D. Plus, pool_size = (3, 3) is an optimal option.
            pool1 = MaxPooling2D(pool_size = (3, 3), strides = (2, 2), padding = "same")(conv1)
            # Regression: Dropout layer is needed.
            pool1 = Dropout(1.0 - dropout_keep_prob)(pool1)
            
            block = pool1
            
            # Regression: For the size of filters, 64 is enough. Higher than 64, more parameters with no enhancement.
            filters = filters_mat[2]
            
        elif input_shape == (256, 256, 1):
            conv1 = _conv_bn_relu(filters=filters_mat[2], kernel_size=(3, 3), strides=(2, 2), is_first_layer = False)(input)
            
            conv2 = _conv_bn_relu(filters=filters_mat[2], kernel_size=(3, 3), strides=(2, 2), is_first_layer = False)(conv1)
            
            pool1 = MaxPooling2D(pool_size = (3, 3), strides = (2, 2), padding = "same")(conv2)
            
            pool1 = Dropout(1.0 - dropout_keep_prob)(pool1)  
            
            block = pool1       
            
            filters = filters_mat[2]
            
        
        for i, r in enumerate(repetitions):
            block = _residual_block(block_fn, filters=filters, repetitions=r, is_first_layer=(i == 0))(block)
            # Regression: Dropout layer is needed.
# =============================================================================
#             block = Dropout(1.0 - dropout_keep_prob)(block)
# =============================================================================
            filters *= 2
        
        block = _bn_relu(block)
        
        block_shape = K.int_shape(block)
# =============================================================================
#         block = GlobalAveragePooling2D(block)
# =============================================================================
        block = AveragePooling2D(pool_size=(block_shape[ROW_AXIS], block_shape[COL_AXIS]),
                                 strides=(1, 1))(block)
        block = Dropout(1.0 - dropout_keep_prob)(block)

        block = Flatten()(block)
        
         ## One more Dense layer
# =============================================================================
#         block = Dense(units=filters_mat[2]*8, kernel_initializer="he_normal",
#                       activation="relu")(block)
#         # Last activation
#         block = Dropout(1.0 - dropout_keep_prob)(block)
# =============================================================================
      ##  
        block = Dense(units=num_outputs, kernel_initializer="he_normal",
                      activation="linear")(block)

        model = Model(inputs=input, outputs=block)
        
        return model

    @staticmethod
    def build_resnet_18(input_shape, num_outputs):
        return ResnetBuilder.build(input_shape, num_outputs, basic_block, [2, 2, 2, 2])

    @staticmethod
    def build_resnet_34(input_shape, num_outputs):
        return ResnetBuilder.build(input_shape, num_outputs, basic_block, [3, 4, 6, 3])

    @staticmethod
    def build_resnet_50(input_shape, num_outputs):
        return ResnetBuilder.build(input_shape, num_outputs, bottleneck, [3, 4, 6, 3])

    @staticmethod
    def build_resnet_101(input_shape, num_outputs):
        return ResnetBuilder.build(input_shape, num_outputs, bottleneck, [3, 4, 23, 3])

    @staticmethod
    def build_resnet_152(input_shape, num_outputs):
        return ResnetBuilder.build(input_shape, num_outputs, bottleneck, [3, 8, 36, 3])
