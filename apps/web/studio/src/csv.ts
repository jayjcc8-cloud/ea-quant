export const csvFields = [
  "timestamp",
  "open",
  "high",
  "low",
  "close",
  "volume",
] as const;
export type CSVField = (typeof csvFields)[number];
export interface ParsedCSV {
  headers: string[];
  rows: string[][];
  parseErrors: string[];
}
export type Mapping = Record<CSVField, string>;
export function parseCSV(text: string): ParsedCSV {
  const rows: string[][] = [],
    errors: string[] = [];
  let row: string[] = [],
    cell = "",
    quoted = false;
  const source = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    if (c === '"') {
      if (quoted && source[i + 1] === '"') {
        cell += '"';
        i++;
      } else quoted = !quoted;
    } else if (c === "," && !quoted) {
      row.push(cell.trim());
      cell = "";
    } else if ((c === "\n" || c === "\r") && !quoted) {
      if (c === "\r" && source[i + 1] === "\n") i++;
      row.push(cell.trim());
      if (row.some(Boolean)) rows.push(row);
      row = [];
      cell = "";
    } else cell += c;
  }
  if (cell || row.length) {
    row.push(cell.trim());
    rows.push(row);
  }
  if (quoted) errors.push("CSV 引号未闭合。");
  const headers = rows.shift() ?? [];
  if (new Set(headers).size !== headers.length || headers.some((h) => !h))
    errors.push("列名为空或重复，请修复后重新选择文件。");
  if (!rows.length) errors.push("文件没有数据行。");
  return { headers, rows, parseErrors: errors };
}
export function autoMap(headers: string[]): Mapping {
  return Object.fromEntries(
    csvFields.map((f) => [
      f,
      headers.find(
        (h) =>
          h.toLowerCase() === f ||
          (f === "timestamp" &&
            ["date", "日期", "time"].includes(h.toLowerCase())),
      ) ?? "",
    ]),
  ) as Mapping;
}
export function validateCSV(csv: ParsedCSV, mapping: Mapping) {
  const errors: { row: number; field: string; message: string }[] =
    csv.parseErrors.map((message) => ({ row: 1, field: "文件", message }));
  for (const f of csvFields)
    if (!mapping[f] || !csv.headers.includes(mapping[f]))
      errors.push({ row: 1, field: f, message: `${f} 尚未映射` });
  if (
    new Set(Object.values(mapping).filter(Boolean)).size !==
    Object.values(mapping).filter(Boolean).length
  )
    errors.push({ row: 1, field: "字段", message: "每个源列只能映射一次。" });
  const samples = csv.rows.map((r) =>
    csvFields.map((f) => r[csv.headers.indexOf(mapping[f])] ?? ""),
  );
  const seen = new Set<string>();
  let previous = "";
  samples.forEach((r, n) => {
    const add = (field: string, message: string) =>
      errors.push({ row: n + 2, field, message });
    if (csv.rows[n].length !== csv.headers.length)
      add("字段", "字段数量与列名不一致");
    const date = r[0];
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(date) ||
      Number.isNaN(Date.parse(date)) ||
      new Date(date).toISOString().slice(0, 10) !== date
    )
      add("timestamp", "需要有效的 YYYY-MM-DD 日期");
    if (seen.has(date)) add("timestamp", "日期重复");
    if (previous && date < previous) add("timestamp", "日期须按升序排列");
    seen.add(date);
    previous = date;
    r.slice(1).forEach((value, i) => {
      if (
        !value.trim() ||
        !Number.isFinite(Number(value)) ||
        Number(value) < 0 ||
        (i < 4 && Number(value) === 0)
      )
        add(csvFields[i + 1], `${csvFields[i + 1]} 缺失或不是有效非负数字`);
    });
    const [, open, high, low, close] = r.map(Number);
    if (high < Math.max(open, close, low) || low > Math.min(open, close, high))
      add("OHLC", "最高/最低价与开收盘价不一致");
  });
  return {
    errors,
    samples,
    start: samples[0]?.[0] ?? "",
    end: samples.at(-1)?.[0] ?? "",
  };
}
