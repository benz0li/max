# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #
"""Implements StaticTuple, a statically-sized uniform container.

You can import these APIs from the `utils` package. For example:

```mojo
from utils import StaticTuple
```
"""


# ===-----------------------------------------------------------------------===#
# Utilities
# ===-----------------------------------------------------------------------===#


@always_inline
fn _set_array_elem[
    index: Int,
    size: Int,
    type: AnyTrivialRegType,
](
    val: type,
    ref array: __mlir_type[`!pop.array<`, size.value, `, `, type, `>`],
):
    """Sets the array element at position `index` with the value `val`.

    Parameters:
        index: the position to replace the value at.
        size: the size of the array.
        type: the element type of the array

    Args:
        val: the value to set.
        array: the array which is captured by reference.
    """
    var ptr = __mlir_op.`pop.array.gep`(
        UnsafePointer(to=array).address, index.value
    )
    UnsafePointer(ptr)[] = val


@always_inline
fn _create_array[
    size: Int, type: AnyTrivialRegType
](lst: VariadicList[type]) -> __mlir_type[
    `!pop.array<`, size.value, `, `, type, `>`
]:
    """Sets the array element at position `index` with the value `val`.

    Parameters:
        size: the size of the array.
        type: the element type of the array

    Args:
        lst: the list of values to set.

    Returns:
        The array with values filled from the input list.
    """

    if len(lst) == 1:
        return __mlir_op.`pop.array.repeat`[
            _type = __mlir_type[`!pop.array<`, size.value, `, `, type, `>`]
        ](lst[0])

    debug_assert(size == len(lst), "mismatch in the number of elements")

    var array: __mlir_type[`!pop.array<`, size.value, `, `, type, `>`]
    __mlir_op.`lit.ownership.mark_initialized`(__get_mvalue_as_litref(array))

    @parameter
    for idx in range(size):
        _set_array_elem[idx, size, type](lst[idx], array)

    return array


# ===-----------------------------------------------------------------------===#
# StaticTuple
# ===-----------------------------------------------------------------------===#


fn _static_tuple_construction_checks[size: Int]():
    """Checks if the properties in `StaticTuple` are valid.

    Validity right now is just ensuring the number of elements is > 0.

    Parameters:
      size: The number of elements.
    """
    constrained[size >= 0, "number of elements in `StaticTuple` must be >= 0"]()


@register_passable("trivial")
struct StaticTuple[element_type: AnyTrivialRegType, size: Int](
    Copyable, Defaultable, Movable, Sized
):
    """A statically sized tuple type which contains elements of homogeneous types.

    Parameters:
        element_type: The type of the elements in the tuple.
        size: The size of the tuple.
    """

    alias type = __mlir_type[
        `!pop.array<`, size.value, `, `, Self.element_type, `>`
    ]
    var array: Self.type
    """The underlying storage for the static tuple."""

    @always_inline
    fn __init__(out self):
        """Constructs an empty (undefined) tuple."""
        _static_tuple_construction_checks[size]()
        __mlir_op.`lit.ownership.mark_initialized`(__get_mvalue_as_litref(self))

    @always_inline
    @implicit
    fn __init__(out self, array: Self.type):
        """Constructs from an array type.

        Args:
            array: Underlying MLIR array type.
        """
        self.array = array

    @always_inline
    @implicit
    fn __init__(out self, *elems: Self.element_type):
        """Constructs a static tuple given a set of arguments.

        Args:
            elems: The element types.
        """
        _static_tuple_construction_checks[size]()
        self.array = _create_array[size](elems)

    @always_inline
    @implicit
    fn __init__(out self, values: VariadicList[Self.element_type]):
        """Creates a tuple constant using the specified values.

        Args:
            values: The list of values.
        """
        _static_tuple_construction_checks[size]()
        self.array = _create_array[size, Self.element_type](values)

    fn __init__(out self, *, other: Self):
        """Explicitly copy the provided StaticTuple.

        Args:
            other: The StaticTuple to copy.
        """
        self.array = other.array

    @always_inline("nodebug")
    fn __len__(self) -> Int:
        """Returns the length of the array. This is a known constant value.

        Returns:
            The size of the list.
        """
        return size

    @always_inline("nodebug")
    fn __getitem__[index: Int](self) -> Self.element_type:
        """Returns the value of the tuple at the given index.

        Parameters:
            index: The index into the tuple.

        Returns:
            The value at the specified position.
        """
        constrained[index < size]()
        var val = __mlir_op.`pop.array.get`[
            _type = Self.element_type,
            index = index.value,
        ](self.array)
        return val

    @always_inline("nodebug")
    fn __getitem__[I: Indexer, //](self, idx: I) -> Self.element_type:
        """Returns the value of the tuple at the given dynamic index.

        Parameters:
            I: A type that can be used as an index.

        Args:
            idx: The index into the tuple.

        Returns:
            The value at the specified position.
        """
        debug_assert(size > index(idx), "index must be within bounds")
        var ptr = __mlir_op.`pop.array.gep`(
            UnsafePointer(to=self.array).address, index(idx)
        )
        return UnsafePointer(ptr)[]

    @always_inline("nodebug")
    fn __setitem__[I: Indexer, //](mut self, idx: I, val: Self.element_type):
        """Stores a single value into the tuple at the specified dynamic index.

        Parameters:
            I: A type that can be used as an index.

        Args:
            idx: The index into the tuple.
            val: The value to store.
        """
        debug_assert(size > index(idx), "index must be within bounds")
        var tmp = self
        var ptr = __mlir_op.`pop.array.gep`(
            UnsafePointer(to=tmp.array).address, index(idx)
        )
        UnsafePointer(ptr)[] = val
        self = tmp

    @always_inline("nodebug")
    fn __setitem__[idx: Int](mut self, val: Self.element_type):
        """Stores a single value into the tuple at the specified index.

        Parameters:
            idx: The index into the tuple.

        Args:
            val: The value to store.
        """
        constrained[idx < size]()
        _set_array_elem[idx, size, Self.element_type](val, self.array)
