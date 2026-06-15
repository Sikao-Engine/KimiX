"""Tests for the check_fmt module."""

from __future__ import annotations

from kimi_cli.tools.file.check_fmt import check_json_text, check_xml_text

# --- JSON tests ---


def test_check_json_text_valid_object():
    """Valid JSON object returns None."""
    assert check_json_text('{"key": "value"}') is None


def test_check_json_text_valid_array():
    """Valid JSON array returns None."""
    assert check_json_text('[1, 2, 3]') is None


def test_check_json_text_valid_string():
    """Valid JSON string returns None."""
    assert check_json_text('"hello"') is None


def test_check_json_text_valid_number():
    """Valid JSON number returns None."""
    assert check_json_text('42') is None


def test_check_json_text_valid_boolean():
    """Valid JSON boolean returns None."""
    assert check_json_text('true') is None


def test_check_json_text_valid_null():
    """Valid JSON null returns None."""
    assert check_json_text('null') is None


def test_check_json_text_valid_nested():
    """Valid nested JSON returns None."""
    assert check_json_text('{"a": {"b": [1, 2, {"c": null}]}}') is None


def test_check_json_text_invalid_syntax():
    """Invalid JSON returns an error message."""
    result = check_json_text('{"key": broken}')
    assert result is not None
    assert "JSON decode error" in result


def test_check_json_text_invalid_trailing_comma():
    """JSON with trailing comma returns an error message."""
    result = check_json_text('{"key": "value",}')
    assert result is not None
    assert "JSON decode error" in result


def test_check_json_text_invalid_unclosed_string():
    """JSON with unclosed string returns an error message."""
    result = check_json_text('{"key": "value}')
    assert result is not None
    assert "JSON decode error" in result


def test_check_json_text_empty_string():
    """Empty string is invalid JSON."""
    result = check_json_text('')
    assert result is not None
    assert "JSON decode error" in result


def test_check_json_text_whitespace_only():
    """Whitespace-only string is invalid JSON."""
    result = check_json_text('   \n\t  ')
    assert result is not None
    assert "JSON decode error" in result


def test_check_json_text_with_callback():
    """json_callback is invoked with parsed object."""
    called_with = []

    def callback(obj):
        called_with.append(obj)

    result = check_json_text('{"key": "value"}', json_callback=callback)
    assert result is None
    assert called_with == [{"key": "value"}]


def test_check_json_text_callback_raises():
    """Exception in json_callback is caught and returned as error."""

    def callback(_):
        raise ValueError("callback error")

    result = check_json_text('{"key": "value"}', json_callback=callback)
    assert result is not None
    assert "failed to validate JSON file" in result
    assert "callback error" in result


def test_check_json_text_unicode():
    """Valid JSON with unicode characters returns None."""
    assert check_json_text('{"emoji": "🎉", "chinese": "你好"}') is None


# --- XML tests ---


def test_check_xml_text_valid_simple():
    """Valid simple XML returns None."""
    assert check_xml_text('<root></root>') is None


def test_check_xml_text_valid_with_content():
    """Valid XML with content returns None."""
    assert check_xml_text('<root>hello</root>') is None


def test_check_xml_text_valid_with_attributes():
    """Valid XML with attributes returns None."""
    assert check_xml_text('<root attr="value">text</root>') is None


def test_check_xml_text_valid_self_closing():
    """Valid self-closing XML returns None."""
    assert check_xml_text('<root><item/></root>') is None


def test_check_xml_text_valid_nested():
    """Valid nested XML returns None."""
    assert check_xml_text('<a><b><c>deep</c></b></a>') is None


def test_check_xml_text_valid_declaration():
    """Valid XML with declaration returns None."""
    assert check_xml_text('<?xml version="1.0"?><root/>') is None


def test_check_xml_text_invalid_unclosed_tag():
    """XML with unclosed tag returns an error message."""
    result = check_xml_text('<root><unclosed></root>')
    assert result is not None
    assert "XML parse error" in result


def test_check_xml_text_invalid_mismatched_tag():
    """XML with mismatched tags returns an error message."""
    result = check_xml_text('<root></wrong>')
    assert result is not None
    assert "XML parse error" in result


def test_check_xml_text_invalid_no_root():
    """XML with no root element returns an error message."""
    result = check_xml_text('just text')
    assert result is not None
    assert "XML parse error" in result


def test_check_xml_text_empty_string():
    """Empty string is invalid XML."""
    result = check_xml_text('')
    assert result is not None
    assert "XML parse error" in result


def test_check_xml_text_whitespace_only():
    """Whitespace-only string is invalid XML."""
    result = check_xml_text('   \n\t  ')
    assert result is not None
    assert "XML parse error" in result


def test_check_xml_text_with_callback():
    """xml_callback is invoked with parsed tree."""
    called_with = []

    def callback(tree):
        called_with.append(tree)

    result = check_xml_text('<root>hello</root>', xml_callback=callback)
    assert result is None
    assert len(called_with) == 1
    assert called_with[0].tag == "root"


def test_check_xml_text_callback_raises():
    """Exception in xml_callback is caught and returned as error."""

    def callback(_):
        raise ValueError("callback error")

    result = check_xml_text('<root/>', xml_callback=callback)
    assert result is not None
    assert "failed to validate XML file" in result
    assert "callback error" in result


def test_check_xml_text_unicode():
    """Valid XML with unicode characters returns None."""
    assert check_xml_text('<root>你好 🎉</root>') is None


# --- Edge cases ---


def test_check_json_text_large():
    """Large valid JSON returns None."""
    data = '{"items": [' + ','.join(str(i) for i in range(1000)) + ']}'
    assert check_json_text(data) is None


def test_check_xml_text_large():
    """Large valid XML returns None."""
    items = ''.join(f'<item>{i}</item>' for i in range(1000))
    assert check_xml_text(f'<root>{items}</root>') is None
