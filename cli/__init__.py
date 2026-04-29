"""mcp-ax CLI — dispatch package.

Subcommands are registered in cli.__main__. This file intentionally keeps the
package import side-effect-free so future beads can hang `lint`, `trace`,
`claim`, `report`, `try`, `baseline`, `fix`, and `pack` modules off the same
namespace without circular-import pain.
"""

REPO_ROOT_MARKER = "harness/rules/_index.yaml"
