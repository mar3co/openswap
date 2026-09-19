import pytest

from openswap.brand_motion import brand_mark_centers, brand_motion_progress


def test_brand_motion_starts_as_ring_and_settles_as_mark():
    assert brand_mark_centers(brand_motion_progress(0.0)) == (16.0, 16.0)
    assert brand_mark_centers(brand_motion_progress(1.0)) == (13.0, 19.0)


def test_brand_motion_uses_the_brand_spring_overshoot():
    assert brand_motion_progress(0.5) > 1.0


@pytest.mark.parametrize("fraction", [-1.0, 2.0])
def test_brand_motion_clamps_time_but_never_the_spring(fraction):
    expected = 0.0 if fraction < 0 else 1.0
    assert brand_motion_progress(fraction) == expected
