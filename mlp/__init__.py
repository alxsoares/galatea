"""
Multilayer Perceptron
"""
__authors__ = "Ian Goodfellow"
__copyright__ = "Copyright 2012-2013, Universite de Montreal"
__credits__ = ["Ian Goodfellow"]
__license__ = "3-clause BSD"
__maintainer__ = "Ian Goodfellow"

from collections import OrderedDict
import numpy as np
import warnings

from theano import config
from theano.gof.op import get_debug_values
from theano.printing import Print
from theano.sandbox.rng_mrg import MRG_RandomStreams
import theano.tensor as T

from pylearn2.costs.cost import Cost
from pylearn2.expr.probabilistic_max_pooling import max_pool_channels
from pylearn2.linear import conv2d
from pylearn2.linear.matrixmul import MatrixMul
from pylearn2.models.model import Model
from pylearn2.space import Conv2DSpace
from pylearn2.space import Space
from pylearn2.space import VectorSpace
from pylearn2.utils import function
from pylearn2.utils import safe_izip
from pylearn2.utils import sharedX
from pylearn2.models.mlp import Layer

class Adaptive(Layer):
    """
        WRITEME
    """

    def __init__(self,
                 dim,
                 layer_name,
                 irange = None,
                 istdev = None,
                 sparse_init = None,
                 sparse_stdev = 1.,
                 include_prob = 1.0,
                 init_bias = None,
                 W_lr_scale = None,
                 b_lr_scale = None,
                 switch_lr_scale = None,
                 mask_weights = None,
                 left_slope = 0.0,
                 copy_input = 0,
                 max_row_norm = None,
                 max_col_norm = None,
                 use_bias = True):
        """

            include_prob: probability of including a weight element in the set
            of weights initialized to U(-irange, irange). If not included
            it is initialized to 0.

            """

        if use_bias and init_bias is None:
            init_bias = 0.

        self.__dict__.update(locals())
        del self.self

        self.switch = sharedX( np.ones((self.dim,)) * 0.5, name = layer_name+'_switch')

        if use_bias:
            self.b = sharedX( np.zeros((self.dim,)) + init_bias, name = layer_name + '_b')
        else:
            assert b_lr_scale is None
            init_bias is None

    def get_lr_scalers(self):

        rval = OrderedDict()

        if self.W_lr_scale is not None:
            W, = self.transformer.get_params()
            rval[W] = self.W_lr_scale

        if self.use_bias and self.b_lr_scale is not None:
            rval[self.b] = self.b_lr_scale

        if self.switch_lr_scale is not None:
            rval[self.switch] = self.switch_lr_scale

        return rval

    def set_input_space(self, space):
        """ Note: this resets parameters! """

        self.input_space = space

        if isinstance(space, VectorSpace):
            self.requires_reformat = False
            self.input_dim = space.dim
        else:
            self.requires_reformat = True
            self.input_dim = space.get_total_dimension()
            self.desired_space = VectorSpace(self.input_dim)

        self.output_space = VectorSpace(self.dim + self.copy_input * self.input_dim)

        rng = self.mlp.rng
        if self.irange is not None:
            assert self.istdev is None
            assert self.sparse_init is None
            W = rng.uniform(-self.irange,
                            self.irange,
                            (self.input_dim, self.dim)) * \
                (rng.uniform(0.,1., (self.input_dim, self.dim))
                 < self.include_prob)
        elif self.istdev is not None:
            assert self.sparse_init is None
            W = rng.randn(self.input_dim, self.dim) * self.istdev
        else:
            assert self.sparse_init is not None
            W = np.zeros((self.input_dim, self.dim))
            def mask_rejects(idx, i):
                if self.mask_weights is None:
                    return False
                return self.mask_weights[idx, i] == 0.
            for i in xrange(self.dim):
                assert self.sparse_init <= self.input_dim
                for j in xrange(self.sparse_init):
                    idx = rng.randint(0, self.input_dim)
                    while W[idx, i] != 0 or mask_rejects(idx, i):
                        idx = rng.randint(0, self.input_dim)
                    W[idx, i] = rng.randn()
            W *= self.sparse_stdev

        W = sharedX(W)
        W.name = self.layer_name + '_W'

        self.transformer = MatrixMul(W)

        W ,= self.transformer.get_params()
        assert W.name is not None

        if self.mask_weights is not None:
            expected_shape =  (self.input_dim, self.dim)
            if expected_shape != self.mask_weights.shape:
                raise ValueError("Expected mask with shape "+str(expected_shape)+" but got "+str(self.mask_weights.shape))
            self.mask = sharedX(self.mask_weights)

    def censor_updates(self, updates):

        switch = self.switch
        if switch in updates:
            updates[switch] = T.clip(updates[switch], 0., 1.)
        if self.mask_weights is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updates[W] = updates[W] * self.mask

        if self.max_row_norm is not None:
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                row_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=1))
                desired_norms = T.clip(row_norms, 0, self.max_row_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + row_norms)).dimshuffle(0, 'x')

        if self.max_col_norm is not None:
            assert self.max_row_norm is None
            W ,= self.transformer.get_params()
            if W in updates:
                updated_W = updates[W]
                col_norms = T.sqrt(T.sum(T.sqr(updated_W), axis=0))
                desired_norms = T.clip(col_norms, 0, self.max_col_norm)
                updates[W] = updated_W * (desired_norms / (1e-7 + col_norms))

    def get_params(self):
        W ,= self.transformer.get_params()
        assert W.name is not None
        rval = self.transformer.get_params()
        assert not isinstance(rval, set)
        rval = list(rval)
        rval.append(self.switch)
        if self.use_bias:
            assert self.b.name is not None
            assert self.b not in rval
            rval.append(self.b)
        return rval

    def get_weight_decay(self, coeff):
        if isinstance(coeff, str):
            coeff = float(coeff)
        assert isinstance(coeff, float) or hasattr(coeff, 'dtype')
        W ,= self.transformer.get_params()
        return coeff * T.sqr(W).sum()

    def get_weights(self):
        if self.requires_reformat:
            # This is not really an unimplemented case.
            # We actually don't know how to format the weights
            # in design space. We got the data in topo space
            # and we don't have access to the dataset
            raise NotImplementedError()
        W ,= self.transformer.get_params()
        return W.get_value()

    def set_weights(self, weights):
        W, = self.transformer.get_params()
        W.set_value(weights)

    def set_biases(self, biases):
        assert self.use_bias
        self.b.set_value(biases)

    def get_biases(self):
        assert self.use_bias
        return self.b.get_value()

    def get_weights_format(self):
        return ('v', 'h')

    def get_weights_topo(self):

        if not isinstance(self.input_space, Conv2DSpace):
            raise NotImplementedError()

        W ,= self.transformer.get_params()

        W = W.T

        W = W.reshape((self.dim, self.input_space.shape[0],
                       self.input_space.shape[1], self.input_space.num_channels))

        W = Conv2DSpace.convert(W, self.input_space.axes, ('b', 0, 1, 'c'))

        return function([], W)()

    def get_monitoring_channels(self):

        W ,= self.transformer.get_params()

        assert W.ndim == 2

        sq_W = T.sqr(W)

        row_norms = T.sqrt(sq_W.sum(axis=1))
        col_norms = T.sqrt(sq_W.sum(axis=0))

        return OrderedDict([
                            ('row_norms_min'  , row_norms.min()),
                            ('row_norms_mean' , row_norms.mean()),
                            ('row_norms_max'  , row_norms.max()),
                            ('col_norms_min'  , col_norms.min()),
                            ('col_norms_mean' , col_norms.mean()),
                            ('col_norms_max'  , col_norms.max()),
                            ('switch_min', self.switch.min()),
                            ('switch_mean', self.switch.mean()),
                            ('switch_max', self.switch.max())
                            ])

    def fprop(self, state_below):

        self.input_space.validate(state_below)

        if self.requires_reformat:
            if not isinstance(state_below, tuple):
                for sb in get_debug_values(state_below):
                    if sb.shape[0] != self.dbm.batch_size:
                        raise ValueError("self.dbm.batch_size is %d but got shape of %d" % (self.dbm.batch_size, sb.shape[0]))
                    assert reduce(lambda x,y: x * y, sb.shape[1:]) == self.input_dim

            state_below = self.input_space.format_as(state_below, self.desired_space)

        z = self.transformer.lmul(state_below)
        if self.use_bias:
            z = z + self.b
        if self.layer_name is not None:
            z.name = self.layer_name + '_z'


        right = (self.switch * z + (1. - self.switch) * T.tanh(z)) * (z > 0.)
        left = z * (z <= .0)
        p = left + right

        if self.copy_input:
            p = T.concatenate((p, state_below), axis=1)

        return p

