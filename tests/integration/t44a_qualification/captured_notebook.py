"""tests/integration/t44a_qualification/captured_notebook.py

Genuine marimo cells over externally bound captured inputs and a native working catalog.
"""

import marimo

__generated_with = "0.25.2"
app = marimo.App()


@app.cell
def _():
    import marimo as mo
    from notebook_fixture import notebook_open

    binding, con, marker = notebook_open()
    return binding, con, marker, mo


@app.cell
def _(binding, con, marker, mo):
    from notebook_fixture import cell_read

    held = cell_read(binding, con, marker)
    mo.md(f"Held total {held[0]}, snapshot {held[1]}, release {held[3]}")
    return cell_read, held


@app.cell
def _(binding, cell_read, con, held, marker, mo):
    fresh = cell_read(binding, con, marker, previous=held)
    mo.md(f"Fresh total {fresh[0]}, snapshot {fresh[1]}, release {fresh[3]}")
    return (fresh,)


if __name__ == "__main__":
    app.run()
