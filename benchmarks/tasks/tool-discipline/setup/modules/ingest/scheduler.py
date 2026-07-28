"""Fixed-interval scheduling, expressed as a plan rather than a loop."""


def plan(jobs, interval_s):
    return [{"job": job, "at_s": index * interval_s}
            for index, job in enumerate(jobs)]
