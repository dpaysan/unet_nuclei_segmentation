# coding=utf-8

"""
Authors:
original cellprofiler plugin Tim Becker, Juan Caicedo, Claire McQuinn 
unet_shape_resize workaround for odd pixel dimensions: Eric Czech 
Modifications to run as standalone code, Keras API upgrade: Volker Hilsenstein

Also see:
https://github.com/CellProfiler/CellProfiler-plugins/issues/65
https://github.com/jr0th/segmentation
https://github.com/carpenterlab/unet4nuclei

License: BSD-3, see LICENSE.md for details

"""
import os.path
import numpy as np
import tifffile
from skimage import transform
from tqdm import tqdm

from skimage.io import imread, imsave
from skimage.measure import label
from skimage.morphology import remove_small_holes, remove_small_objects
import keras

__doc__ = """\
ClassifyPixels-Unet calculates pixel wise classification using an UNet 
network. The default network model is trained to identify nuclei, background 
and the nuclei boundary. Classification results are returned as three channel 
images: 

* red channel stores background classification
* green channel stores nuclei classification
* blue channel stores boundary classification

In the simplest use case, the classifications are converted to gray value images 
using the module ColorToGray. The module IdentifyPrimaryObjects can be 
used to identify example images in the nuclei channel (green channel). 


Author: Tim Becker, Juan Caicedo, Claire McQuinn 
"""

from src.utils import get_file_list

option_dict_conv = {"activation": "relu", "padding": "same"}
option_dict_bn = { "momentum": 0.9}


class UnetSegmenter(object):
    def __init__(self, weight_file:str=None, close_holes:int=0,
                 remove_objects:int=1e10, boundary_boost_factor:float=1.0):
        super().__init__()

        self.weight_file=weight_file
        self.close_holes=close_holes
        self.remove_objects=remove_objects

        self.boundary_boost_factor = boundary_boost_factor

        self.model = None



    def initialize(self, input_shape, automated_shape_adjustment=False):
        unet_shape = self.unet_shape_resize(input_shape)
        if input_shape != unet_shape and not automated_shape_adjustment:
            raise ValueError(
                f"Shape {input_shape} not compatible with 3 max-pool layers. Consider setting "
                f"automated_shape_adjustment=True.")

        # create model
        dim1, dim2 = unet_shape
        self.get_model_3_class(dim1, dim2)

        # load weights
        if self.weight_file is not None:
            self.model.load_weights(self.weight_file)


    def unet_shape_resize(self, image_shape):
        """Resize shape for compatibility with UNet architecture

        Args:
            shape: Shape of images to be resized in format HW[D1, D2, ...] where any
                trailing dimensions after the first two are ignored
            n_pooling_layers: Number of pooling (or upsampling) layers in network
        Returns:
            Shape with HW sizes transformed to nearest value acceptable by network
        """
        base = 2 ** 3
        rcsh = np.round(np.array(image_shape[:2]) / base).astype(int)
        # Combine HW axes transformation with trailing shape dimensions
        # (being careful not to return 0-length axes)
        return tuple(base * np.clip(rcsh, 1, None)) + tuple(image_shape[2:])

    def unet_image_resize(self, image):
        """Resize image for compatibility with UNet architecture

        Args:
            image: Image to be resized in format HW[D1, D2, ...] where any
                trailing dimensions after the first two are ignored
            n_pooling_layers: Number of pooling (or upsampling) layers in network
        Returns:
            Image with HW dimensions resized to nearest value acceptable by network
        Reference:
            https://github.com/CellProfiler/CellProfiler-plugins/issues/65
        """
        shape = self.unet_shape_resize(image.shape)
        # Note here that the type and range of the image will either not change
        # or become float64, 0-1 (which makes no difference w/ subsequent min/max scaling)
        return image if shape == image.shape else transform.resize(
            image, shape)

    def classify(self, input_image, resize_to_model=True):
        dim1, dim2 = input_image.shape
        mdim1, mdim2 = self.model.input_shape[1:3]
        needs_resize = False if (dim1, dim2) == (mdim1, mdim2) else True
        if needs_resize:
            if resize_to_model:
                input_image = transform.resize(input_image, (mdim1, mdim2))  # , anti_aliasing=True)
            else:
                raise ValueError("image size does not match model size, set resize_to_model=True")
        images = input_image.reshape((-1, mdim1, mdim2, 1))

        # scale min, max to [0.0,1.0]
        images = images.astype(np.float32)
        images = images - np.min(images)
        images = images.astype(np.float32) / np.max(images)

        pixel_classification = self.model.predict(images, batch_size=1)

        retval = pixel_classification[0, :, :, :]
        if needs_resize:
            retval = transform.resize(retval, (dim1, dim2, retval.shape[2]))
        return retval

    def pred_to_label(self, pred):
        pred = np.argmax(pred * [1,1,self.boundary_boost_factor], -1) == 1
        pred = remove_small_holes(pred, self.close_holes)
        pred = remove_small_objects(pred, self.remove_objects)
        labels = label(pred)
        return labels

    def get_model_3_class(self, dim1, dim2, activation="softmax"):
        [x, y] = self.get_core(dim1, dim2)

        y = keras.layers.Conv2D(3, (1, 1), **option_dict_conv)(y)

        if activation is not None:
            y = keras.layers.Activation(activation)(y)

        self.model = keras.models.Model(x, y)


    def get_core(self, dim1, dim2):
        x = keras.layers.Input(shape=(dim1, dim2, 1))

        a = keras.layers.Conv2D(64, (3, 3), **option_dict_conv)(x)
        a = keras.layers.BatchNormalization(**option_dict_bn)(a)

        a = keras.layers.Conv2D(64, (3, 3), **option_dict_conv)(a)
        a = keras.layers.BatchNormalization(**option_dict_bn)(a)

        y = keras.layers.MaxPooling2D()(a)

        b = keras.layers.Conv2D(128, (3, 3), **option_dict_conv)(y)
        b = keras.layers.BatchNormalization(**option_dict_bn)(b)

        b = keras.layers.Conv2D(128, (3, 3), **option_dict_conv)(b)
        b = keras.layers.BatchNormalization(**option_dict_bn)(b)

        y = keras.layers.MaxPooling2D()(b)

        c = keras.layers.Conv2D(256, (3, 3), **option_dict_conv)(y)
        c = keras.layers.BatchNormalization(**option_dict_bn)(c)

        c = keras.layers.Conv2D(256, (3, 3), **option_dict_conv)(c)
        c = keras.layers.BatchNormalization(**option_dict_bn)(c)

        y = keras.layers.MaxPooling2D()(c)

        d = keras.layers.Conv2D(512, (3, 3), **option_dict_conv)(y)
        d = keras.layers.BatchNormalization(**option_dict_bn)(d)

        d = keras.layers.Conv2D(512, (3, 3), **option_dict_conv)(d)
        d = keras.layers.BatchNormalization(**option_dict_bn)(d)

        # UP

        d = keras.layers.UpSampling2D()(d)
        y = keras.layers.merge.concatenate([d, c], axis=3)

        e = keras.layers.Conv2D(256, (3, 3), **option_dict_conv)(y)
        e = keras.layers.BatchNormalization(**option_dict_bn)(e)

        e = keras.layers.Conv2D(256, (3, 3), **option_dict_conv)(e)
        e = keras.layers.BatchNormalization(**option_dict_bn)(e)

        e = keras.layers.UpSampling2D()(e)

        y = keras.layers.merge.concatenate([e, b], axis=3)

        f = keras.layers.Conv2D(128, (3, 3), **option_dict_conv)(y)
        f = keras.layers.BatchNormalization(**option_dict_bn)(f)

        f = keras.layers.Conv2D(128, (3, 3), **option_dict_conv)(f)
        f = keras.layers.BatchNormalization(**option_dict_bn)(f)

        f = keras.layers.UpSampling2D()(f)

        y = keras.layers.merge.concatenate([f, a], axis=3)

        y = keras.layers.Conv2D(64, (3, 3), **option_dict_conv)(y)
        y = keras.layers.BatchNormalization(**option_dict_bn)(y)

        y = keras.layers.Conv2D(64, (3, 3), **option_dict_conv)(y)
        y = keras.layers.BatchNormalization(**option_dict_bn)(y)

        return [x, y]

    def segment_image_dir(self, input_dir:str, output_dir:str, resize_to_model:bool=True):
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        file_list = get_file_list(input_dir)
        for file in tqdm(file_list):
            image = tifffile.imread(file)
            pred = self.classify(image, resize_to_model=resize_to_model)
            label = self.pred_to_label(pred)
            file_name = os.path.split(file)[1]
            file_path = os.path.join(output_dir, file_name)
            tifffile.imsave(file_path, label)





