from typing import Hashable, Optional, Sequence, Tuple, Union

import jax
import jax.lax as lax
import jax.numpy as jnp
from jaxtyping import Array, Bool, Float

from .._module import Module, static_field
from ._stateful import State, StateIndex


class BatchNorm(Module):
    r"""Computes a mean and standard deviation over the batch and spatial
    dimensions of an array, and uses these to normalise the whole array. Optionally
    applies a channelwise affine transformation afterwards.

    Given an input array $x = [x_1, ... x_C]$ with $C$ channels, this layer computes

    $$\frac{x_i - \mathbb{E}[x_i]}{\sqrt{\text{Var}[x_i] + \varepsilon}} * \gamma_i + \beta_i$$

    for all $i$. Here $*$ denotes elementwise multiplication and $\gamma$, $\beta$ have
    shape $(C,)$ if `channelwise_affine=True` and $\gamma = 1$, $\beta = 0$ if
    `channelwise_affine=False`. Expectations are computed over all spatial dimensions
    *and* over the batch dimension, and updated batch-by-batch according to `momentum`.

    !!! example

        See [this example](../../examples/stateful.ipynb) for example usage.

    !!! warning

        This layer must be used inside of a `vmap` or `pmap` with a matching
        `axis_name`. (Not doing so will raise a `NameError`.)

    Note that this layer behaves differently during training and inference. During
    training then statistics are computed using the input data, and the running
    statistics updated. During inference then just the running statistics are used.
    Whether the model is in training or inference mode should be toggled using
    [`equinox.tree_inference`][].
    """  # noqa: E501

    weight: Optional[Float[Array, "input_size"]]
    bias: Optional[Float[Array, "input_size"]]
    first_time_index: StateIndex[Bool[Array, ""]]
    state_index: StateIndex[
        Tuple[Float[Array, "input_size"], Float[Array, "input_size"]]
    ]
    axis_name: Union[Hashable, Sequence[Hashable]]
    inference: bool
    input_size: int = static_field()
    eps: float = static_field()
    channelwise_affine: bool = static_field()
    momentum: float = static_field()

    def __init__(
        self,
        input_size: int,
        axis_name: Union[Hashable, Sequence[Hashable]],
        eps: float = 1e-5,
        channelwise_affine: bool = True,
        momentum: float = 0.99,
        inference: bool = False,
        **kwargs,
    ):
        """**Arguments:**

        - `input_size`: The number of channels in the input array.
        - `axis_name`: The name of the batch axis to compute statistics over, as passed
            to `axis_name` in `jax.vmap` or `jax.pmap`. Can also be a sequence (e.g. a
            tuple or a list) of names, to compute statistics over multiple named axes.
        - `eps`: Value added to the denominator for numerical stability.
        - `channelwise_affine`: Whether the module has learnable channel-wise affine
            parameters.
        - `momentum`: The rate at which to update the running statistics. Should be a
            value between 0 and 1 exclusive.
        - `inference`: If `False` then the batch means and variances will be calculated
            and used to update the running statistics. If `True` then the running
            statistics are directly used for normalisation. This may be toggled with
            [`equinox.tree_inference`][] or overridden during
            [`equinox.nn.BatchNorm.__call__`][].
        """

        super().__init__(**kwargs)

        if channelwise_affine:
            self.weight = jnp.ones((input_size,))
            self.bias = jnp.zeros((input_size,))
        else:
            self.weight = None
            self.bias = None
        self.first_time_index = StateIndex(lambda **_: jnp.array(True))
        make_buffers = lambda **_: (jnp.empty((input_size,)), jnp.empty((input_size,)))
        self.state_index = StateIndex(make_buffers)
        self.inference = inference
        self.axis_name = axis_name
        self.input_size = input_size
        self.eps = eps
        self.channelwise_affine = channelwise_affine
        self.momentum = momentum

    def __call__(
        self,
        x: Array,
        state: State,
        *,
        key: Optional["jax.random.PRNGKey"] = None,  # pyright: ignore
        inference: Optional[bool] = None,
    ) -> Tuple[Array, State]:
        """**Arguments:**

        - `x`: A JAX array of shape `(input_size, dim_1, ..., dim_N)`.
        - `state`: An [`equinox.nn.State`][] object (which is used to store the
            running statistics).
        - `key`: Ignored; provided for compatibility with the rest of the Equinox API.
            (Keyword only argument.)
        - `inference`: As per [`equinox.nn.BatchNorm.__init__`][]. If
            `True` or `False` then it will take priority over `self.inference`. If
            `None` then the value from `self.inference` will be used.

        **Returns:**

        A 2-tuple of:

        - A JAX array of shape `(input_size, dim_1, ..., dim_N)`.
        - An updated context object (storing the updated running statistics).

        **Raises:**

        A `NameError` if no `vmap`s are placed around this operation, or if this vmap
        does not have a matching `axis_name`.
        """

        if inference is None:
            inference = self.inference
        if inference:
            running_mean, running_var = state.get(self.state_index)
        else:

            def _stats(y):
                mean = jnp.mean(y)
                mean = lax.pmean(mean, self.axis_name)
                var = jnp.mean((y - mean) ** 2)
                var = lax.pmean(var, self.axis_name)
                var = jnp.maximum(0.0, var)
                return mean, var

            first_time = state.get(self.first_time_index)
            state = state.set(self.first_time_index, jnp.array(False))

            batch_mean, batch_var = jax.vmap(_stats)(x)
            running_mean, running_var = state.get(self.state_index)
            momentum = self.momentum
            running_mean = (1 - momentum) * batch_mean + momentum * running_mean
            running_var = (1 - momentum) * batch_var + momentum * running_var
            running_mean = lax.select(first_time, batch_mean, running_mean)
            running_var = lax.select(first_time, batch_var, running_var)
            state = state.set(self.state_index, (running_mean, running_var))

        def _norm(y, m, v, w, b):
            out = (y - m) / jnp.sqrt(v + self.eps)
            if self.channelwise_affine:
                out = out * w + b
            return out

        out = jax.vmap(_norm)(x, running_mean, running_var, self.weight, self.bias)
        return out, state
