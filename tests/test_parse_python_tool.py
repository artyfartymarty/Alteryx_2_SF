"""A Python tool parses to a `python` node whose config carries the script; it is a `snowpark`-class tool,
so a workflow that has one is written as a `build.py`."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from parsers import plugin_map, tool_config

XML = """<Configuration><Script>import pandas as pd
df = Alteryx.read("#1")
Alteryx.write(df, 1)</Script></Configuration>"""


def test_plugin_maps_to_python_with_one_input_and_five_outputs():
    assert plugin_map.classify("AlteryxBasePluginsGui.PythonTool.PythonTool", None) == "python"
    anchors = plugin_map.anchors_of("python")
    assert anchors["in"] == ["Input"]
    assert anchors["out"] == {"Output1": "1", "Output2": "2", "Output3": "3", "Output4": "4", "Output5": "5"}


def test_config_carries_the_script_verbatim():
    config = tool_config.parse_config("python", ET.fromstring(XML))
    assert config == {"script": 'import pandas as pd\ndf = Alteryx.read("#1")\nAlteryx.write(df, 1)'}


def test_a_python_node_is_snowpark_class_so_its_workflow_is_a_python_build():
    assert plugin_map.node_class("python") == "snowpark"
    assert plugin_map.node_class("filter") == "sql"
