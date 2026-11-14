"""Small data-cleaning exercise for the Waypoint IDE pilot."""


def mean_reading(values):
    valid = [value for value in values if value is not None]
    return sum(valid) / len(values)  # Fix the failing missing-value test.


if __name__ == "__main__":
    readings = [10, None, 20]
    print(mean_reading(readings))
