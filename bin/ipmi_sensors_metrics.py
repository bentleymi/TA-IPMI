#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipmi_common

if __name__ == "__main__":
    sys.exit(ipmi_common.main("metrics"))
