# pipeline_runner.py

import subprocess
import sys
from config import PIPELINE_DIR


def run_script(script_name):

    result = subprocess.run(
        [sys.executable, script_name],
        cwd=PIPELINE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    output = ""

    if result.stdout:
        output += result.stdout

    if result.stderr:
        output += "\nERROR:\n" + result.stderr

    return output