"""Read each job's `needs` from a GitHub workflow without a YAML library."""
import re


def jobs(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    blocks = re.split(r"(?m)^  (?=[A-Za-z0-9_-]+:\s*$)", body)
    for block in blocks:
        header = re.match(r"([A-Za-z0-9_-]+):\s*\n", block)
        if header is None:
            continue
        needs = re.search(r"(?m)^    needs:\s*(.+)$", block)
        names: list[str] = []
        if needs:
            names = [n.strip() for n in needs.group(1).strip("[] ").split(",") if n.strip()]
        result[header.group(1)] = names
    return result
