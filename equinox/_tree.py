from typing import Any, Callable, Optional, Sequence, TYPE_CHECKING, Union

import jax.tree_util as jtu
import numpy as np
from jaxtyping import Array, Bool, PyTree

from ._custom_types import sentinel
from ._doc_utils import doc_repr
from ._filters import is_array


if TYPE_CHECKING:
    _Node = Any
else:
    _Node = doc_repr(Any, "Node")


class _LeafWrapper:
    def __init__(self, value: Any):
        self.value = value


def _remove_leaf_wrapper(x: _LeafWrapper) -> Any:
    assert type(x) is _LeafWrapper
    return x.value


class _CountedIdDict:
    def __init__(self, keys, values):
        assert len(keys) == len(values)
        self._dict = {id(k): v for k, v in zip(keys, values)}
        self._count = {id(k): 0 for k in keys}

    def __contains__(self, item):
        return id(item) in self._dict

    def __getitem__(self, item):
        self._count[id(item)] += 1
        return self._dict[id(item)]

    def get(self, item, default):
        try:
            return self[item]
        except KeyError:
            return default

    def count(self, item):
        return self._count[id(item)]


def tree_at(
    where: Callable[[PyTree], Union[_Node, Sequence[_Node]]],
    pytree: PyTree,
    replace: Union[Any, Sequence[Any]] = sentinel,
    replace_fn: Callable[[_Node], Any] = sentinel,
    is_leaf: Optional[Callable[[Any], bool]] = None,
):
    """Updates a PyTree out-of-place; a bit like using `.at[].set()` on a JAX array.

    **Arguments:**

    - `where`: A callable `PyTree -> Node` or `PyTree -> Sequence[Node]`. It should
        consume a PyTree with the same structure as `pytree`, and return the node or
        nodes that should be replaced. For example
        `where = lambda mlp: mlp.layers[-1].linear.weight`.
    - `pytree`: The PyTree to modify.
    - `replace`: Either a single element, or a sequence of the same length as returned
        by `where`. This specifies the replacements to make at the locations specified
        by `where`. Mutually exclusive with `replace_fn`.
    - `replace_fn`: A function `Node -> Any`. It will be called on every node specified
        by `where`. The return value from `replace_fn` will be used in its place.
        Mutually exclusive with `replace`.
    - `is_leaf`: As `jtu.tree_flatten`. For example pass `is_leaf=lambda x: x is None`
        to be able to replace `None` values using `tree_at`.

    **Returns:**

    A copy of the input PyTree, with the appropriate modifications.

    !!! example

        This is useful for performing model surgery. See the [Tricks](../../../tricks)
        page.
    """  # noqa: E501

    # We need to specify a particular node in a PyTree.
    # This is surprisingly difficult to do! As far as I can see, pretty much the only
    # way of doing this is to specify e.g. `x.foo[0].bar` via `is`, and then pulling
    # a few tricks to try and ensure that the same object doesn't appear multiple
    # times in the same PyTree.
    #
    # So this first `tree_map` serves a dual purpose.
    # 1) Makes a copy of the composite nodes in the PyTree, to avoid aliasing via
    #    e.g. `pytree=[(1,)] * 5`. This has the tuple `(1,)` appear multiple times.
    # 2) It makes each leaf be a unique Python object, as it's wrapped in
    #    `_LeafWrapper`. This is needed because Python caches a few builtin objects:
    #    `assert 0 + 1 is 1`. I think only a few leaf types are subject to this.
    # So point 1) should ensure that all composite nodes are unique Python objects,
    # and point 2) should ensure that all leaves are unique Python objects.
    # Between them, all nodes of `pytree` are handled.
    #
    # I think pretty much the only way this can fail is when using a custom node with
    # singleton-like flatten+unflatten behaviour, which is pretty edge case. And we've
    # added a check for it at the bottom of this function, just to be sure.
    #
    # Whilst we're here: we also double-check that `where` is well-formed and doesn't
    # use leaf information. (As else `node_or_nodes` will be wrong.)
    node_or_nodes_nowrapper = where(pytree)
    pytree = jtu.tree_map(_LeafWrapper, pytree, is_leaf=is_leaf)
    node_or_nodes = where(pytree)
    leaves1, structure1 = jtu.tree_flatten(node_or_nodes_nowrapper, is_leaf=is_leaf)
    leaves2, structure2 = jtu.tree_flatten(node_or_nodes)
    leaves2 = [_remove_leaf_wrapper(x) for x in leaves2]
    if (
        structure1 != structure2
        or len(leaves1) != len(leaves2)
        or any(l1 is not l2 for l1, l2 in zip(leaves1, leaves2))
    ):
        raise ValueError(
            "`where` must use just the PyTree structure of `pytree`. `where` must not "
            "depend on the leaves in `pytree`."
        )
    del node_or_nodes_nowrapper, leaves1, structure1, leaves2, structure2

    # Normalise whether we were passed a single node or a sequence of nodes.
    in_pytree = False

    def _in_pytree(x):
        nonlocal in_pytree
        if x is node_or_nodes:  # noqa: F821
            in_pytree = True
        return x  # needed for jax.tree_util.Partial, which has a dodgy constructor

    jtu.tree_map(_in_pytree, pytree, is_leaf=lambda x: x is node_or_nodes)  # noqa: F821
    if in_pytree:
        nodes = (node_or_nodes,)
        if replace is not sentinel:
            replace = (replace,)
    else:
        nodes = node_or_nodes
    del in_pytree, node_or_nodes

    # Normalise replace vs replace_fn
    if replace is sentinel:
        if replace_fn is sentinel:
            raise ValueError(
                "Precisely one of `replace` and `replace_fn` must be specified."
            )
        else:

            def _replace_fn(x):
                x = jtu.tree_map(_remove_leaf_wrapper, x)
                return replace_fn(x)

            replace_fns = [_replace_fn] * len(nodes)
    else:
        if replace_fn is sentinel:
            if len(nodes) != len(replace):
                raise ValueError(
                    "`where` must return a sequence of leaves of the same length as "
                    "`replace`."
                )
            replace_fns = [lambda _, r=r: r for r in replace]
        else:
            raise ValueError(
                "Precisely one of `replace` and `replace_fn` must be specified."
            )
    node_replace_fns = _CountedIdDict(nodes, replace_fns)

    # Actually do the replacement
    def _make_replacement(x: _Node) -> Any:
        return node_replace_fns.get(x, _remove_leaf_wrapper)(x)

    out = jtu.tree_map(
        _make_replacement, pytree, is_leaf=lambda x: x in node_replace_fns
    )

    # Check that `where` is well-formed.
    for node in nodes:
        count = node_replace_fns.count(node)
        if count == 0:
            raise ValueError(
                "`where` does not specify an element or elements of `pytree`."
            )
        elif count == 1:
            pass
        else:
            raise ValueError(
                "`where` does not uniquely identify a single element of `pytree`. This "
                "usually occurs when trying to replace a `None` value:\n"
                "\n"
                "  >>> eqx.tree_at(lambda t: t[0], (None, None, 1), True)\n"
                "\n"
                "\n"
                "for which the fix is to specify that `None`s should be treated as "
                "leaves:\n"
                "\n"
                "  >>> eqx.tree_at(lambda t: t[0], (None, None, 1), True,\n"
                "  ...             is_leaf=lambda x: x is None)"
            )

    return out


def tree_equal(*pytrees: PyTree) -> Union[bool, np.bool_, Bool[Array, ""]]:
    """Returns `True` if all input PyTrees are equal. Every PyTree must have the same
    structure. Any JAX or NumPy arrays (as leaves) must have the same shape, dtype, and
    values to be considered equal. JAX arrays and NumPy arrays are considered equal
    to each other.

    If used under JIT then this may return a tracer.

    **Arguments:**

    - `*pytrees`: Any number of PyTrees each with any structure.

    **Returns:**

    A boolean, or bool-typed tracer.
    """
    flat, treedef = jtu.tree_flatten(pytrees[0])
    out = True
    for pytree in pytrees[1:]:
        flat_, treedef_ = jtu.tree_flatten(pytree)
        if treedef_ != treedef:
            return False
        for elem, elem_ in zip(flat, flat_):
            if is_array(elem):
                if is_array(elem_):
                    if (elem.shape != elem_.shape) or (elem.dtype != elem_.dtype):
                        return False
                    allsame = (elem == elem_).all()
                    if allsame is False:
                        return False
                    out = out & allsame
                else:
                    return False
            else:
                if is_array(elem_):
                    return False
                else:
                    if elem != elem_:
                        return False
    return out


def _inferences(pytree):
    is_leaf = lambda x: hasattr(x, "inference") and x is not pytree

    out = [pytree.inference] if hasattr(pytree, "inference") else []

    leaves = [x for x in jtu.tree_leaves(pytree, is_leaf=is_leaf) if is_leaf(x)]
    # Nodes with an inference flag might have sub-nodes with an inference flag.

    for x in leaves:
        out.extend(_inferences(x))
    return out


def tree_inference(pytree: PyTree, value: bool) -> PyTree:
    """Convenience function for setting all `inference` attributes on a PyTree.

    `inference` flags are used to toggle the behaviour of a number of the pre-built
    neural network layers, such as [`equinox.nn.Dropout`][] or
    [`equinox.nn.BatchNorm`][].

    !!! Example

        ```python
        class Model(eqx.Module):
            norm: eqx.nn.BatchNorm
            dropout: eqx.nn.Dropout
            linear: eqx.nn.Linear

            def __init__(self, key):
                key1, key2 = jax.random.split(key)
                self.norm = eqx.nn.BatchNorm(3, "batch", key=key1)
                self.dropout = eqx.nn.Dropout(0.4)
                self.linear = eqx.nn.Linear(3, 1, key=key2)

            def __call__(self, x, ctx, *, key):
                x, ctx = self.norm(x, ctx)
                x = self.dropout(x, key=key)
                x = self.linear(x)
                return x, ctx

        training_model = Model(jax.random.PRNGKey(0))
        inference_model = eqx.tree_inference(training_model, value=True)
        training_model_again = eqx.tree_inference(inference_model, value=False)
        ```

    Equivalent to:
    ```python
    has_inference = lambda leaf: hasattr(leaf, "inference")

    def where(pytree):
        return tuple(x.inference
                     for x in jtu.tree_leaves(pytree, is_leaf=has_inference)
                     if has_inference(x))

    equinox.tree_at(where, pytree, replace_fn=lambda _: value)
    ```

    **Arguments:**

    - `pytree`: the PyTree to modify.
    - `value`: the value to set all `inference` attributes to.

    **Returns:**

    A copy of `pytree` with all `inference` flags set to `value`.
    """
    return tree_at(_inferences, pytree, replace_fn=lambda _: value)
