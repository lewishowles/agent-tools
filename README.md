# Agent tools

A collection of independently installed command-line tools that make common agent tasks faster, safer, or substantially smaller in context than using ordinary human tools directly.

## Package layout

The root `pyproject.toml` is a virtual uv workspace. It discovers packages under `packages/`, but it is not a package itself.

Each package owns its own:

- `pyproject.toml`
- version
- dependencies
- command-line entry points
- source code and tests

Packages are independent. The repository does not provide a shared application framework, base class, or version.

```text
packages/
└── <package-name>/
    └── pyproject.toml
```

## Add an independent package

1. Create `packages/<package-name>/`.
2. Add a package-owned `pyproject.toml` with the package name, version, dependencies, and command-line entry points.
3. Add the package's source code and tests inside its directory.
4. Run `uv lock --dry-run` from the repository root to confirm the workspace finds the package.
