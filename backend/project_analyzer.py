"""
Project Analyzer — Vision-based project understanding

Uses Claude's vision capabilities to:
- Analyze architecture diagrams/screenshots
- Understand codebase structure
- Identify tech debt and testing gaps
- Suggest missions based on visual analysis
"""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional, List
import uuid

from db import get_db

log = logging.getLogger("devfleet.project_analyzer")


async def analyze_project_files(
    project_path: str,
    files_to_analyze: Optional[List[str]] = None,
    custom_prompt: str = ""
) -> dict:
    """
    Analyze a project by examining files, diagrams, and code structure.

    Args:
        project_path: Path to the project
        files_to_analyze: Specific files to analyze (READMEs, architecture docs, images)
        custom_prompt: Additional context about what to look for

    Returns: Analysis with insights and suggested missions
    """
    try:
        from claude_code_sdk import query as sdk_query, ClaudeCodeOptions

        # Build analysis prompt
        analysis_prompt = _build_analysis_prompt(project_path, files_to_analyze, custom_prompt)

        options = ClaudeCodeOptions(
            model="claude-opus-4-7",
            permission_mode="bypassPermissions",
            max_turns=1,
            cwd=project_path,
        )

        output_parts = []
        async for message in sdk_query(prompt=analysis_prompt, options=options):
            if message is None:
                continue
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        output_parts.append(block.text)
            elif hasattr(message, "result") and message.result:
                output_parts.append(message.result)

        response = "\n".join(output_parts).strip()

        # Parse JSON response
        try:
            analysis = json.loads(response)
        except json.JSONDecodeError:
            if "```json" in response:
                analysis = json.loads(response.split("```json")[1].split("```")[0])
            elif "```" in response:
                analysis = json.loads(response.split("```")[1].split("```")[0])
            else:
                analysis = {"raw_analysis": response}

        return analysis

    except ImportError:
        log.warning("Claude SDK not available, falling back to basic analysis")
        return _basic_project_analysis(project_path)


def _build_analysis_prompt(project_path: str, files: Optional[List[str]], custom: str) -> str:
    """Build the vision analysis prompt."""

    file_context = ""

    if not files:
        # Look for common documentation/diagram files
        files = []
        for pattern in ["README*", "ARCHITECTURE*", "*.md", "*.png", "*.jpg", "*.svg"]:
            files.extend(Path(project_path).glob(f"**/{pattern}"))
        files = [str(f) for f in files][:5]  # Limit to 5 files

    for file_path in files[:5]:
        if os.path.isfile(file_path):
            try:
                if file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                    # Read image and encode as base64
                    with open(file_path, 'rb') as f:
                        b64 = base64.b64encode(f.read()).decode()
                    file_context += f"\n[IMAGE: {Path(file_path).name}]\nBase64 image data (see vision context)\n"
                else:
                    # Read text file
                    with open(file_path, 'r', errors='ignore') as f:
                        content = f.read()[:1000]  # First 1000 chars
                    file_context += f"\n[FILE: {Path(file_path).name}]\n{content}\n"
            except Exception as e:
                log.debug(f"Could not read {file_path}: {e}")

    return f"""Analyze this project and provide structural insights.

## Project Path
{project_path}

## Project Files/Context
{file_context or "No specific files provided"}

## Custom Context
{custom or "General analysis"}

## Your Analysis

Provide a JSON response with:
{{
  "project_type": "Type of project (web app, API, library, etc.)",
  "tech_stack": ["technology1", "technology2"],
  "architecture_summary": "Brief description of project structure",
  "key_components": ["component1", "component2"],
  "testing_status": "Assessment of testing coverage",
  "documentation_status": "Assessment of documentation",
  "identified_issues": ["issue1", "issue2"],
  "tech_debt": ["debt1", "debt2"],
  "suggested_missions": [
    {{
      "title": "Mission title",
      "type": "feature|test|refactor|fix|docs",
      "priority": "high|medium|low",
      "effort": "hours estimate",
      "reason": "Why this is needed"
    }}
  ],
  "recommendations": ["recommendation1"]
}}"""


def _basic_project_analysis(project_path: str) -> dict:
    """Fallback: basic file-based analysis without vision."""

    analysis = {
        "project_type": "unknown",
        "tech_stack": [],
        "architecture_summary": "Analysis requires vision capabilities",
        "key_components": [],
        "testing_status": "unknown",
        "documentation_status": "unknown",
        "identified_issues": [],
        "tech_debt": [],
        "suggested_missions": [],
        "recommendations": ["Enable Claude vision to get detailed analysis"]
    }

    # Simple heuristics
    files = set()
    for root, dirs, filenames in os.walk(project_path):
        # Skip hidden dirs and common ignore patterns
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', 'venv', '.venv']]
        files.update(filenames)

    # Detect tech stack by file presence
    if 'package.json' in files:
        analysis["tech_stack"].append("Node.js/JavaScript")
    if 'requirements.txt' in files or 'setup.py' in files:
        analysis["tech_stack"].append("Python")
    if 'Dockerfile' in files:
        analysis["tech_stack"].append("Docker")
    if 'go.mod' in files:
        analysis["tech_stack"].append("Go")
    if 'Cargo.toml' in files:
        analysis["tech_stack"].append("Rust")

    # Check for tests
    has_tests = any('test' in f.lower() or 'spec' in f.lower() for f in files)
    analysis["testing_status"] = "Has test files" if has_tests else "No obvious test files"

    # Check for documentation
    has_docs = any('readme' in f.lower() or 'doc' in f.lower() or f.endswith('.md') for f in files)
    analysis["documentation_status"] = "Some documentation present" if has_docs else "No documentation found"

    return analysis
