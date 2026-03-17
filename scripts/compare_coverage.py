#!/usr/bin/env python3
"""
Compare coverage between main and current branch for top-level src/ folders.
Generates a Markdown report showing coverage differences.
"""

import json
import sys
from pathlib import Path

# Paths to coverage JSON files
MAIN_COV = "coverage-main.json"
BRANCH_COV = "coverage-branch.json"

# Source root
SRC_ROOT = Path("src")

# Maximum allowed coverage drop (in percentage points) before failing the CI
COVERAGE_DROP_THRESHOLD = 1.0

FAILURE_EMOJI = "🚫"


def get_top_level_folders(src_root: Path) -> list[str]:
    """
    Dynamically get the first-level folders in src/
    Excludes hidden folders and build artifacts.
    """
    if not src_root.exists():
        return []

    excluded_patterns = [".", "__pycache__", ".egg-info", ".dist-info"]

    folders = []
    for p in src_root.iterdir():
        if not p.is_dir():
            continue
        # Skip hidden folders and build artifacts
        if any(pattern in p.name for pattern in excluded_patterns):
            continue
        folders.append(p.name)

    return sorted(folders)


def load_coverage(path: str) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """
    Load coverage JSON and aggregate coverage per top-level folder.

    Aggregates coverage correctly by summing covered_lines and num_statements
    across all files in each folder, then calculating the percentage.
    This matches the calculation method used by lcov/genhtml.

    Coverage JSON format from pytest-cov:
    {
        "meta": {...},
        "files": {
            "src/module/file.py": {
                "executed_lines": [1, 2, 3, ...],
                "missing_lines": [10, 11, ...],
                "excluded_lines": [],
                "summary": {
                    "covered_lines": 50,
                    "num_statements": 60,
                    "percent_covered": 83.33,
                    "missing_lines": 10,
                    "excluded_lines": 0
                }
            }
        },
        "totals": {...}
    }

    Returns:
        Tuple of (folder_percentages, folder_stats)
        - folder_percentages: dict mapping folder name to coverage percentage
        - folder_stats: dict mapping folder name to {"covered": int, "total": int}
    """
    coverage_path = Path(path)
    if not coverage_path.exists():
        print(f"Warning: Coverage file {path} not found", file=sys.stderr)
        return {}, {}

    try:
        with coverage_path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse {path}: {e}", file=sys.stderr)
        return {}, {}

    # Store covered_lines and num_statements per folder
    folder_stats: dict[str, dict[str, int]] = {}

    files = data.get("files", {})
    if not files:
        print(f"Warning: No files found in {path}", file=sys.stderr)
        return {}, {}

    for file_path, file_data in files.items():
        # Normalize path and extract top-level folder
        try:
            path_obj = Path(file_path)
            # Handle both absolute and relative paths
            if path_obj.is_absolute() or not str(file_path).startswith("src/"):
                # Try to find 'src' in the path
                parts = path_obj.parts
                if "src" in parts:
                    src_idx = parts.index("src")
                    if src_idx + 1 < len(parts):
                        top_folder = parts[src_idx + 1]
                    else:
                        continue
                else:
                    continue
            else:
                # Relative path starting with src/
                rel = path_obj.relative_to(SRC_ROOT)
                if len(rel.parts) == 0:
                    continue
                top_folder = rel.parts[0]

            # Extract coverage stats (lines, not percentages!)
            summary = file_data.get("summary", {})
            covered_lines = summary.get("covered_lines", 0)
            num_statements = summary.get("num_statements", 0)

            if top_folder not in folder_stats:
                folder_stats[top_folder] = {"covered": 0, "total": 0}

            folder_stats[top_folder]["covered"] += covered_lines
            folder_stats[top_folder]["total"] += num_statements

        except (ValueError, IndexError) as e:
            # Skip files outside src/ or with invalid structure
            print(f"Debug: Skipping {file_path}: {e}", file=sys.stderr)
            continue

    # Calculate coverage percentage per folder
    result = {}
    for folder, stats in folder_stats.items():
        total = stats["total"]
        if total > 0:
            result[folder] = (stats["covered"] / total) * 100.0
        else:
            result[folder] = 0.0

    return result, folder_stats


def format_diff(diff: float) -> str:
    """Format diff with appropriate sign and emoji."""
    if diff > 0:
        return f"🟢 +{diff:.1f}%"
    if diff < 0:
        return f"🔴 {diff:.1f}%"
    return f"⚪ {diff:.1f}%"


def main() -> int:
    """Generate coverage comparison report. Returns non-zero if coverage dropped beyond threshold."""
    # Dynamically get folders
    top_level_folders = get_top_level_folders(SRC_ROOT)

    if not top_level_folders:
        print("Error: No top-level folders found in src/", file=sys.stderr)
        return 1

    # Load coverage data (returns percentages and raw stats)
    main_cov, main_stats = load_coverage(MAIN_COV)
    branch_cov, branch_stats = load_coverage(BRANCH_COV)

    if not branch_cov:
        print("Error: No coverage data for current branch", file=sys.stderr)
        return 1

    # Use all top-level folders from src/ directory
    # This ensures we show all packages, even those without coverage
    all_folders = top_level_folders

    # Generate markdown report
    report = ["## 📊 Code Coverage Diff vs `main`", "", "| Folder | Main | Branch | Δ |", "| --- | --: | --: | --- |"]

    for folder in all_folders:
        main_val = main_cov.get(folder, 0.0)
        branch_val = branch_cov.get(folder, 0.0)
        diff = branch_val - main_val

        diff_str = format_diff(diff)

        # Show N/A for folders with no coverage data
        main_str = f"{main_val:.1f}%" if main_val > 0 else "0.0%"
        branch_str = f"{branch_val:.1f}%" if branch_val > 0 else "0.0%"

        report.append(f"| `{folder}` | {main_str} | {branch_str} | {diff_str} |")

    report.append("")

    # Add summary - calculate weighted overall coverage
    coverage_dropped = False
    overall_diff = 0.0
    if main_stats and branch_stats:
        # Calculate overall coverage from raw stats (not averaging percentages)
        main_total_covered = sum(stats["covered"] for stats in main_stats.values())
        main_total_lines = sum(stats["total"] for stats in main_stats.values())
        main_overall = (main_total_covered / main_total_lines * 100) if main_total_lines > 0 else 0.0

        branch_total_covered = sum(stats["covered"] for stats in branch_stats.values())
        branch_total_lines = sum(stats["total"] for stats in branch_stats.values())
        branch_overall = (branch_total_covered / branch_total_lines * 100) if branch_total_lines > 0 else 0.0

        overall_diff = branch_overall - main_overall

        report.append(f"**Overall:** {main_overall:.1f}% → {branch_overall:.1f}% ({format_diff(overall_diff)})")

        # Check if coverage dropped beyond the allowed threshold
        if overall_diff < -COVERAGE_DROP_THRESHOLD:
            coverage_dropped = True
            report.append("")
            report.append(
                f"> **{FAILURE_EMOJI} Coverage dropped by {abs(overall_diff):.1f}%** "
                f"(threshold: {COVERAGE_DROP_THRESHOLD}%).\n"
                f"> Please add tests for the changed code before merging."
            )

    # Print the report (for GitHub Actions step)
    print("\n".join(report))

    if coverage_dropped:
        return 2  # Distinct code to indicate coverage regression

    return 0


if __name__ == "__main__":
    sys.exit(main())
