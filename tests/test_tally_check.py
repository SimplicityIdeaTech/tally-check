import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tally_check as tc  # noqa: E402


def write(folder: Path, name: str, rows, bom=False):
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{name}.csv", "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        csv.writer(f).writerows(rows)


INV = [["id", "year", "amount", "updated_at"],
       ["1", "2025", "10.10", "a"], ["2", "2025", "20.20", "a"], ["3", "2026", "5.00", "a"]]


def setup(tmp_path, old=INV, new=None):
    write(tmp_path / "old", "invoices", old)
    write(tmp_path / "new", "invoices", new if new is not None else old)
    return str(tmp_path / "old"), str(tmp_path / "new")


def run(capsys, *args):
    rc = tc.main(list(args))
    return rc, capsys.readouterr().out


def test_identical_exit_0(tmp_path, capsys):
    o, n = setup(tmp_path)
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 0 and "OVERALL: PASS" in out


def test_missing_row(tmp_path, capsys):
    o, n = setup(tmp_path, new=INV[:-1])
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 2 and "keys missing in new" in out and "(3)" in out


def test_changed_amount(tmp_path, capsys):
    new = [r[:] for r in INV]
    new[2][2] = "20.00"
    o, n = setup(tmp_path, new=new)
    rc, out = run(capsys, o, n)
    assert rc == 2 and "sum(amount)" in out and "-0.20" in out


def test_decimal_not_float(tmp_path, capsys):
    rows = [["id", "amount"]] + [[str(i), "0.1"] for i in range(10)]
    o, n = setup(tmp_path, old=rows)
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 0 and "old 1.0 " in out


def test_map_renamed_table(tmp_path, capsys):
    write(tmp_path / "old", "invoices", INV)
    write(tmp_path / "new", "bills", INV)
    o, n = str(tmp_path / "old"), str(tmp_path / "new")
    rc, _ = run(capsys, o, n)
    assert rc == 1  # table only on one side each
    rc, out = run(capsys, o, n, "--map", "invoices=bills", "--key", "invoices=id")
    assert rc == 0 and "invoices -> bills" in out


def test_key_changed_and_extra(tmp_path, capsys):
    new = [r[:] for r in INV] + [["9", "2026", "1.00", "a"]]
    new[1][2] = "10.10 "  # trailing space only: must not count as changed
    new[2][2] = "20.21"
    o, n = setup(tmp_path, new=new)
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 2
    assert "rows with changed content: old -  new 1" in out
    assert "keys extra in new: old -  new 1" in out


def test_ignore_cols(tmp_path, capsys):
    new = [r[:] for r in INV]
    new[1][3] = "changed"
    o, n = setup(tmp_path, new=new)
    assert run(capsys, o, n, "--key", "invoices=id")[0] == 2
    assert run(capsys, o, n, "--key", "invoices=id", "--ignore-cols", "updated_at")[0] == 0


def test_group_by(tmp_path, capsys):
    old = [["id", "acct", "amount"], ["1", "A", "10"], ["2", "A", "20"], ["3", "B", "5"]]
    new = [r[:] for r in old]
    new[3][1] = "A"  # moves 5 from B to A; totals unchanged
    o, n = setup(tmp_path, old=old, new=new)
    assert run(capsys, o, n)[0] == 1  # totals match, but no --key so WARN only
    rc, out = run(capsys, o, n, "--group-by", "invoices=acct")
    assert rc == 2 and "sum(amount) [acct=B]" in out


def test_sqlite_input(tmp_path, capsys):
    paths = []
    for name, amt in (("old.db", 1.5), ("new.db", 1.5)):
        p = tmp_path / name
        con = sqlite3.connect(p)
        con.execute("create table invoices (id integer, amount real, note text)")
        con.execute("insert into invoices values (1, ?, null)", (amt,))
        con.commit()
        con.close()
        paths.append(str(p))
    assert run(capsys, *paths, "--key", "invoices=id")[0] == 0
    con = sqlite3.connect(paths[1])
    con.execute("update invoices set amount = 2.5")
    con.commit()
    con.close()
    assert run(capsys, *paths)[0] == 2


def test_sqlite_vs_csv(tmp_path, capsys):
    p = tmp_path / "o.db"
    con = sqlite3.connect(p)
    con.execute("create table invoices (id, year, amount, updated_at)")
    con.executemany("insert into invoices values (?,?,?,?)", [tuple(r) for r in INV[1:]])
    con.commit()
    con.close()
    write(tmp_path / "new", "invoices", INV)
    assert run(capsys, str(p), str(tmp_path / "new"), "--key", "invoices=id")[0] == 0


def test_bom_and_empty_file(tmp_path, capsys):
    write(tmp_path / "old", "invoices", INV, bom=True)
    write(tmp_path / "new", "invoices", INV)
    (tmp_path / "old" / "empty.csv").write_text("")
    (tmp_path / "new" / "empty.csv").write_text("")
    rc, out = run(capsys, str(tmp_path / "old"), str(tmp_path / "new"), "--key", "invoices=id")
    assert rc == 1 and "PASS  invoices" in out and "[FAIL]" not in out  # WARN only for the unkeyed empty table


def test_dropped_column_is_warning(tmp_path, capsys):
    new = [r[:3] for r in INV]
    o, n = setup(tmp_path, new=new)
    rc, out = run(capsys, o, n)
    assert rc == 1 and "column dropped" in out


def test_thousands_separator_warns(tmp_path, capsys):
    rows = [["id", "amount"], ["1", "1,000.00"]]
    o, n = setup(tmp_path, old=rows)
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 1 and "sum(amount)" in out and "non-numeric" in out


def test_csv_and_md_output(tmp_path, capsys):
    o, n = setup(tmp_path, new=INV[:-1])
    out_csv, out_md = tmp_path / "t.csv", tmp_path / "t.md"
    assert run(capsys, o, n, "--csv", str(out_csv), "--md", str(out_md))[0] == 2
    rows = list(csv.reader(open(out_csv, encoding="utf-8-sig")))
    assert rows[0][:6] == ["table", "check", "old", "new", "diff", "status"]
    assert any(r[1] == "row count" and r[5] == "FAIL" for r in rows)
    assert "OVERALL: FAIL" in out_md.read_text()


def test_bad_args(tmp_path, capsys):
    o, n = setup(tmp_path)
    assert tc.main([o]) == 3
    assert tc.main([o, str(tmp_path / "nope")]) == 3
    assert tc.main([o, n, "--map", "x=y"]) == 3
    assert tc.main([o, n, "--key", "nosuch=id"]) == 3
    assert tc.main([o, n, "--key", "invoices"]) == 3
    assert tc.main([o, n, "--encoding", "nope"]) == 3
    (tmp_path / "junk.txt").write_text("hello")
    assert tc.main([o, str(tmp_path / "junk.txt")]) == 3
    assert run(capsys, o, n, "--key", "invoices=nocol")[0] == 2


def test_inputs_not_modified(tmp_path, capsys):
    o, n = setup(tmp_path)
    before = (Path(o, "invoices.csv").read_bytes(), Path(n, "invoices.csv").read_bytes())
    run(capsys, o, n, "--key", "invoices=id")
    assert before == (Path(o, "invoices.csv").read_bytes(), Path(n, "invoices.csv").read_bytes())


def amounts(vals):
    return [["id", "amount"]] + [[str(i), v] for i, v in enumerate(vals, 1)]


def test_nonnumeric_never_drops_sum_any_order(tmp_path, capsys):
    for old_vals, new_vals in ((["N/A", "10", "20"], ["N/A", "10", "21"]),
                               (["10", "20", "N/A"], ["10", "21", "N/A"])):
        d = tmp_path / ("x" + str(len(list(tmp_path.iterdir()))))
        d.mkdir(exist_ok=True)
        o, n = setup(d, old=amounts(old_vals), new=amounts(new_vals))
        rc, out = run(capsys, o, n, "--key", "invoices=id")
        assert rc == 2 and "e.g. 'N/A'" in out and "[FAIL] sum(amount)" in out
    d = tmp_path / "same"
    d.mkdir()
    o, n = setup(d, old=amounts(["N/A", "10"]))
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 1 and "[WARN] sum(amount)" in out


def test_semicolon_delimiter_detected_and_flag(tmp_path, capsys):
    for side in ("old", "new"):
        d = tmp_path / side
        d.mkdir()
        (d / "invoices.csv").write_text("id;amount\n1;10.5\n2;4\n")
    o, n = str(tmp_path / "old"), str(tmp_path / "new")
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 0 and "sum(amount)" in out
    rc, out = run(capsys, o, n, "--key", "invoices=id", "--delimiter", ",")
    assert rc == 2 and "delimiter" in out


def test_empty_column_in_new_fails(tmp_path, capsys):
    new = [r[:] for r in INV]
    for r in new[1:]:
        r[2] = ""
    o, n = setup(tmp_path, new=new)
    rc, out = run(capsys, o, n, "--key", "invoices=id")
    assert rc == 2 and "all values empty in new" in out


def test_ragged_rows_warn(tmp_path, capsys):
    for side in ("old", "new"):
        d = tmp_path / side
        d.mkdir()
        (d / "invoices.csv").write_text("id,amount\n1,10\n2,5,extra\n")
    rc, out = run(capsys, str(tmp_path / "old"), str(tmp_path / "new"), "--key", "invoices=id")
    assert rc == 1 and "wrong field count" in out and "line 3" in out


def test_csv_injection_guard(tmp_path, capsys):
    rows = [["id", "name"], ["=evil", "x"], ["2", "y"]]
    o, n = setup(tmp_path, old=rows, new=[rows[0], rows[2]])
    out_csv = tmp_path / "t.csv"
    run(capsys, o, n, "--key", "invoices=id", "--csv", str(out_csv))
    text = out_csv.read_text(encoding="utf-8-sig")
    assert "'=evil" in text and ",=evil" not in text
    assert tc.safe_cell("-45.00") == "-45.00" and tc.safe_cell("@x") == "'@x"


def test_no_key_warns_not_pass(tmp_path, capsys):
    o, n = setup(tmp_path)
    rc, out = run(capsys, o, n)
    assert rc == 1 and "no --key" in out and "WARN  invoices" in out


def test_id_columns_not_summed_unless_asked(tmp_path, capsys):
    rows = [["id", "company_id", "amount"], ["1", "7", "5"]]
    o, n = setup(tmp_path, old=rows)
    _, out = run(capsys, o, n, "--key", "invoices=id")
    assert "sum(company_id)" not in out and "sum(amount)" in out
    _, out = run(capsys, o, n, "--key", "invoices=id", "--sum-cols", "invoices=company_id")
    assert "sum(company_id)" in out and "sum(amount)" not in out


def test_sqlite_views_warn(tmp_path, capsys):
    paths = []
    for name in ("a.db", "b.db"):
        p = tmp_path / name
        con = sqlite3.connect(p)
        con.execute("create table invoices (id integer, amount real)")
        con.execute("create view v as select * from invoices")
        con.commit()
        con.close()
        paths.append(str(p))
    rc, out = run(capsys, *paths, "--key", "invoices=id")
    assert rc == 1 and "views not compared" in out


def test_encoding_flag(tmp_path, capsys):
    for side in ("old", "new"):
        d = tmp_path / side
        d.mkdir()
        (d / "invoices.csv").write_bytes("id,name\n1,Caf\xe9\n".encode("latin-1"))
    o, n = str(tmp_path / "old"), str(tmp_path / "new")
    assert tc.main([o, n]) == 3
    assert tc.main([o, n, "--encoding", "latin-1", "--key", "invoices=id"]) == 0


def test_markdown_escapes_newline(tmp_path, capsys):
    rows = [["id", "v"], ["a\nb", "x"]]
    o, n = setup(tmp_path, old=rows, new=[rows[0]])
    md = tmp_path / "t.md"
    run(capsys, o, n, "--key", "invoices=id", "--md", str(md))
    assert all(l.startswith(("|", "#", "**")) or not l.strip() for l in md.read_text().splitlines())
