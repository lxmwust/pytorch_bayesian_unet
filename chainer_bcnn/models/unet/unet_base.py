from __future__ import absolute_import

import copy
import warnings
import chainer
import chainer.functions as F

from .. import Model
from ._helper import conv, _default_conv_param
from ._helper import upconv, _default_upconv_param
from ._helper import norm, _default_norm_param
from ._helper import pool, _default_pool_param
from ._helper import activation, _default_activation_param
from ._helper import dropout, _default_dropout_param
from ._helper import initializer
from ...functions import crop

class UNetBaseBlock(chainer.Chain):
    """ Base class of U-Net convolution blocks
    """
    def __init__(self,
                 ndim,
                 nfilter,
                 conv_param,
                 norm_param,
                 activation_param,
                 ninner=2,
                 residual=False):

        super(UNetBaseBlock, self).__init__()

        self._ndim = ndim
        self._nfilter = nfilter

        self._conv_param = conv_param
        self._norm_param = norm_param
        self._activation_param = activation_param

        self._ninner= ninner
        self._residual = residual

        self._activation = activation(activation_param)

        with self.init_scope():

            for i in range(ninner):
                self.add_link('conv_%d' % i, conv(ndim, None, nfilter, conv_param))

                if norm_param is not None:
                    self.add_link('conv_norm_%d' % i, norm(nfilter, norm_param))

    def __call__(self, x):

        if not self._residual:

            h = x

            for i in range(self._ninner):
                h = self['conv_%d' % i](h)

                if self._norm_param is not None:
                    h = self['conv_norm_%d' % i](h)

                h = self._activation(h)

            return h

        else:

            h = x

            for i in range(self._ninner):
                h = self['conv_%d' % i](h)

                if self._norm_param is not None:
                    h = self['conv_norm_%d' % i](h)

                if i == 0:
                    g = F.identity(h) # TODO: order should be checked

                if i != (self._ninner - 1):
                    h = self._activation(h)

            return self._activation(g + h)


class UNetContractionBlock(UNetBaseBlock):
    pass

class UNetExpansionBlock(UNetBaseBlock):

    def __init__(self,
                 ndim,
                 conv_nfilter,
                 conv_param,
                 upconv_nfilter,
                 upconv_param,
                 norm_param,
                 activation_param,
                 ninner=2,
                 residual=False):

        super(UNetExpansionBlock, self).__init__(
                                ndim,
                                conv_nfilter,
                                conv_param,
                                norm_param,
                                activation_param,
                                ninner,
                                residual)

        self._upconv_param = upconv_param

        with self.init_scope():

            self.add_link('upconv',
                        upconv(ndim, None, upconv_nfilter, upconv_param))

            if norm_param is not None:
                self.add_link('upconv_norm',
                            norm(upconv_nfilter, norm_param))


    def __call__(self, low, high):

        h = self['upconv'](low)
        if self._norm_param is not None:
            h = self['upconv_norm'](h)
        h = self._activation(h)

        h = crop(h, high.shape)
        h = F.concat([h, high], axis=1) # NOTE: fuse

        h = super().__call__(h)

        return h


class UNetBase(Model):
    """ Base class of U-Net model

    Args:
        ndim (int): Number of spatial dimensions.
        nlayer (int, optional): Number of layers.
            Defaults to 5.
        nfilter (list or int, optional): Number of filters.
            Defaults to 32.
        ninner (list or int, optional): Number of layers in UNetBlock.
            Defaults to 2.
        conv_param (dict, optional): Hyperparameter of convolution layer.
            Defaults to {'name':'conv', 'ksize': 3, 'stride': 1, 'pad': 1,
             'initialW': {'name': 'he_normal', 'scale': 1.0}, 'initial_bias': {'name': 'zero'}}.
        pool_param (dict, optional): Hyperparameter of pooling layer.
            Defaults to {'name': 'max', 'ksize': 2, 'stride': 2}.
        upconv_param (dict, optional): Hyperparameter of up-convolution layer.
            Defaults to {'name':'deconv', 'ksize': 3, 'stride': 2, 'pad': 0,
             'initialW': {'name': 'bilinear', 'scale': 1.0}, 'initial_bias': {'name': 'zero'}}.
        norm_param (dict or None, optional): Hyperparameter of normalization layer.
            Defaults to {'name': 'batch'}.
        activation_param (dict, optional): Hyperparameter of activation layer.
            Defaults to {'name': 'relu'}.
        dropout_param (dict or None, optional): Hyperparameter of dropout layer.
            Defaults to {'name': 'dropout', 'ratio': .5}.
        residual (bool, optional): Enable the residual learning.
            Defaults to False.
        return_all_latent (bool, optional): Return all representative feature maps.
            Defaults to False.

    See: https://arxiv.org/pdf/1505.04597.pdf
    """
    def __init__(self,
                 ndim,
                 nlayer=5,
                 nfilter=32,
                 ninner=2,
                 conv_param=_default_conv_param,
                 pool_param=_default_pool_param,
                 upconv_param=_default_upconv_param,
                 norm_param=_default_norm_param,
                 activation_param=_default_activation_param,
                 dropout_param=_default_dropout_param,
                 residual=False,
                 return_all_latent=False,
                ):

        super(UNetBase, self).__init__()

        self._args = locals()

        self._ndim = ndim
        self._nlayer = nlayer

        if isinstance(nfilter, int):
            nfilter = [nfilter*(2**i) for i in range(nlayer)]
        assert len(nfilter) == nlayer
        self._nfilter = nfilter

        if isinstance(ninner, int):
            ninner = [ninner]*nlayer
        assert len(ninner) == nlayer
        self._ninner = ninner

        self._conv_param = conv_param
        self._pool_param = pool_param
        self._upconv_param = upconv_param
        self._norm_param = norm_param
        self._activation_param = activation_param,
        self._dropout_param = dropout_param

        self._residual = residual
        self._return_all_latent = return_all_latent

        self._pool = pool(pool_param)
        self._activation = activation(activation_param)
        self._dropout = dropout(dropout_param)

        with self.init_scope():

            # down
            for i in range(nlayer):

                self.add_link('contraction_block_%d' % i,
                            UNetContractionBlock(ndim,
                                        nfilter[i], conv_param,
                                        norm_param, activation_param,
                                        ninner[i], residual))

            # up
            for i in range(nlayer - 1):

                self.add_link('expansion_block_%d' % i,
                            UNetExpansionBlock(ndim,
                                        nfilter[i], conv_param,
                                        nfilter[i+1], upconv_param,
                                        norm_param, activation_param,
                                        ninner[i], residual))

    def forward(self, x):

        stored_activations = {}

        h = x

        # down
        for i in range(self._nlayer):

            if i != 0:
                h = self._pool(h)

            h = self['contraction_block_%d' % (i)](h)
            h = self._dropout(h)

            stored_activations['contraction_block_%d' % (i)] = h

        # up
        for i in reversed(range(self._nlayer - 1)):

            l = stored_activations['contraction_block_%d' % (i)]

            h = self['expansion_block_%d' % i](h, l)
            h = self._dropout(h)

            stored_activations['expansion_block_%d' % (i)] = h

        if self._return_all_latent: # TODO: memory usage should be checked
            return h, stored_activations

        return h