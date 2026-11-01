import json
import sys


def main(path: str) -> int:
    with open(path, encoding="utf-8") as file:
        sarif = json.load(file)
    dropped = 0
    for run in sarif.get("runs", []):
        results = run.get("results", [])
        kept = [result for result in results if not result.get("suppressions")]
        dropped += len(results) - len(kept)
        run["results"] = kept
    with open(path, "w", encoding="utf-8") as file:
        json.dump(sarif, file)
    print(f"dropped {dropped} findings suppressed in source")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
