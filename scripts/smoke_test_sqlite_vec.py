"""Smoke test sqlite-vec : DB en mémoire, 5 vecteurs dim 3, top-1 retrieve."""

import sqlite3
import sys

import sqlite_vec
import structlog

log = structlog.get_logger()

VECTORS: list[tuple[int, list[float]]] = [
    (1, [1.0, 0.0, 0.0]),
    (2, [0.0, 1.0, 0.0]),
    (3, [0.0, 0.0, 1.0]),
    (4, [1.0, 1.0, 0.0]),
    (5, [0.0, 1.0, 1.0]),
]

QUERY_VEC: list[float] = [0.0, 0.0, 0.95]
EXPECTED_ROWID: int = 3


def main() -> int:
    log.info("smoke.sqlite_vec.start")

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("CREATE VIRTUAL TABLE smoke_vec USING vec0(embedding float[3])")

    for rowid, vec in VECTORS:
        conn.execute(
            "INSERT INTO smoke_vec(rowid, embedding) VALUES (?, ?)",
            (rowid, sqlite_vec.serialize_float32(vec)),
        )

    cur = conn.execute(
        "SELECT rowid, distance FROM smoke_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
        (sqlite_vec.serialize_float32(QUERY_VEC),),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        log.error("smoke.sqlite_vec.no_result")
        print("FAIL : aucun résultat retourné")
        return 1

    top_rowid, distance = row
    log.info("smoke.sqlite_vec.result", rowid=top_rowid, distance=distance)

    if top_rowid != EXPECTED_ROWID:
        print(f"FAIL : attendu rowid={EXPECTED_ROWID}, obtenu rowid={top_rowid}")
        return 1

    print(f"OK : top-1 rowid={top_rowid}, distance={distance:.4f}")
    log.info("smoke.sqlite_vec.success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
