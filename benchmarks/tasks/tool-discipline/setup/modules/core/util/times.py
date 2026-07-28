"""Time helpers, all UTC."""

import datetime


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(moment=None):
    return (moment or now()).strftime("%Y-%m-%dT%H:%M:%SZ")
