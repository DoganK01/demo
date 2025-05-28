import textwrap
from typing import List
import re


def render_input_nicely(raw_input: str, width: int = 80) -> str:

    def format_markdown_block(block: str) -> str:
        lines = block.splitlines() 
        formatted_lines: List[str] = []

        for line in lines:
            stripped_line = line.strip()
            if not stripped_line:
                formatted_lines.append("") 
                continue

            indent_str = line[:len(line) - len(line.lstrip(' '))]

            if stripped_line.startswith("- ") or stripped_line.startswith("* "):
                formatted_lines.append(indent_str + "🔹 " + stripped_line[2:])
            elif re.match(r"\d+\.", stripped_line):
                 match = re.match(r"(\d+\.)(.*)", stripped_line)
                 if match:
                    num, text = match.groups()
                    formatted_lines.append(f"{indent_str}🔢 {num}{text}")
                 else:
                    formatted_lines.append(indent_str + "🔢 " + stripped_line)
            else:

                formatted_lines.append(textwrap.fill(stripped_line, width=width))
        return "\n".join(formatted_lines)

    def format_section(title: str, content: str, emoji: str = "📋") -> str:
        formatted_content = format_markdown_block(content)
        underline_length = len(title) + 2
        return f"\n{emoji} {title}\n" + "-" * underline_length + f"\n{formatted_content}\n"

    blocks = raw_input.split('---')
    result = []

    for block in blocks:
        l_stripped_block = block.lstrip() 
        trimmed_block = block.strip()     

        if not trimmed_block:
            continue

        try:
            lines = l_stripped_block.splitlines()
            first_line = lines[0].strip()
        except IndexError:
            continue

        if l_stripped_block.startswith("### "):
            title_line, _, content = l_stripped_block.partition('\n')
            section_title = title_line.replace("### ", "").strip()
            formatted = format_section(section_title, content, emoji="📋")
            result.append(formatted)
        elif first_line.startswith("**") and first_line.endswith(":**"):
            title_line, _, content = l_stripped_block.partition('\n')
            title = title_line.strip()[2:-3].strip()
            formatted = format_section(title, content, emoji="✨")
            result.append(formatted)
        else:
            result.append(format_markdown_block(block))

    return "\n\n".join(result).strip()
