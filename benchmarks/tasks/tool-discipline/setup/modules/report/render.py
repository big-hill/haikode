"""Plain text rendering of a report dict."""


def render(report):
    width = max([len(k) for k in report] or [1])
    return "\n".join("%-*s %d" % (width, k, v) for k, v in sorted(report.items()))
