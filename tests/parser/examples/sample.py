#!/usr/bin/env python3
"""A sample Python module with various comment styles."""

# This is a line comment
import os  # inline comment

'''
A block string used as a comment
with multiple lines
'''

def hello(name: str) -> str:
    """Greet someone with a friendly message.
    
    Args:
        name: The person's name.
    
    Returns:
        A greeting string.
    """
    url = "http://example.com#fragment"  # trailing comment
    return f"Hello, {name}!"  # f-string with # inside - should not break

# Last comment
