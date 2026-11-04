"""Small, explicitly paired task implementations; not an agent framework.

Only the synthetic plumbing check is implemented until public tasks are available.
Add a pair of hand-written functions for each real task after inspecting its
question and input; never use its gold file to construct predictions.
"""


def setup(case, strategy):
    if case != "synthetic_sales":
        raise ValueError(f"No implementation for case {case!r}")
    if strategy == "pandas":
        import pandas as pd

        def solve(task_dir, output_path):
            source = task_dir / "context" / "sales.csv"
            frame = pd.read_csv(source, dtype={"region": str, "category": str, "amount": "int64"})
            result = (frame.loc[frame["category"] == "food"]
                      .groupby("region", sort=True, as_index=False)["amount"].sum())
            result.to_csv(output_path, index=False, lineterminator="\n")

        return solve

    if strategy == "duckdb":
        import duckdb

        connection = duckdb.connect(":memory:", config={"threads": 1})

        def solve(task_dir, output_path):
            source = str(task_dir / "context" / "sales.csv")
            query = """
                SELECT region, SUM(CAST(amount AS BIGINT)) AS amount
                FROM read_csv(?, header=true, all_varchar=true)
                WHERE category = 'food'
                GROUP BY region ORDER BY region
            """
            rows = connection.execute(query, [source]).fetchall()
            import csv
            with output_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(["region", "amount"])
                writer.writerows(rows)

        return solve
    raise ValueError(f"Unknown strategy: {strategy!r}")
