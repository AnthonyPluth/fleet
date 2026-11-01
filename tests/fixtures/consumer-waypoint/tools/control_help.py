import sys

COMMANDS = {"start": "Start the demo server.", "stop": "Stop it."}

sys.stdout.write("# tools/control\n\n" + "".join(f"- `{name}`: {text}\n" for name, text in COMMANDS.items()))
