from nab_resolver.values import SlottedValue


class Point(SlottedValue):
    __slots__ = __match_args__ = ("x", "y")

    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y


class OtherPoint(Point):
    __slots__ = ()


def test_declared_fields_define_equality_hashing_and_representation() -> None:
    point = Point(1, 2)
    assert point == Point(1, 2)
    assert point != Point(1, 3)
    assert hash(point) == hash(Point(1, 2))
    assert {point: "value"}[Point(1, 2)] == "value"
    assert repr(point) == "Point(x=1, y=2)"


def test_value_comparison_preserves_exact_class_identity() -> None:
    assert Point(1, 2) != OtherPoint(1, 2)
    assert Point(1, 2) != object()
