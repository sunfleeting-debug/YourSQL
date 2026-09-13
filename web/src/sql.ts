/** 编辑器语句分隔；字符串、引用名和注释规则与后端 Lexer 对齐。 */
export interface StatementRange {from: number; to: number}
export function statements(sql: string): StatementRange[] {
  const ranges: StatementRange[] = []
  let start = 0, quote = '', comment = '', meaningful = false
  for (let index = 0; index < sql.length; index++) {
    const char = sql[index], pair = sql.slice(index, index + 2)
    if (comment === 'line') {if ('\r\n'.includes(char)) comment = ''}
    else if (comment === 'block') {if (pair === '*/') {comment = ''; index++}}
    else if (quote) {
      if (char === '\\' && quote === "'") index++
      else if (char === quote) {if (sql[index + 1] === quote) index++; else quote = ''}
    } else if (pair === '--' || pair === '/*') {comment = pair === '--' ? 'line' : 'block'; index++}
    else if ("'\"`".includes(char)) {quote = char; meaningful = true}
    else if (char === ';') {
      if (meaningful) ranges.push({from: start, to: index + 1})
      start = index + 1; meaningful = false
    } else if (!/\s/.test(char)) meaningful = true
  }
  if (meaningful || comment === 'block') ranges.push({from: start, to: sql.length})
  return ranges.map(range => ({...range, from: range.from + (sql.slice(range.from, range.to).match(/^\s*/)?.[0].length ?? 0)}))
}

export function currentStatement(sql: string, cursor: number): StatementRange | undefined {
  const ranges = statements(sql)
  return ranges.find(range => cursor <= range.to) ?? ranges.at(-1)
}

/** 仅格式化词间空白；字符串、引用标识符和注释必须逐字保持。 */
export function formatSQL(sql: string, keywords: string[]): string {
  const known = new Set(keywords.map(word => word.toUpperCase()))
  const tokens = sql.match(/--[^\r\n]*(?:\r?\n|$)|\/\*[\s\S]*?\*\/|'(?:''|\\[\s\S]|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\s+|[A-Za-z_][A-Za-z0-9_$]*|[^\s]/g) ?? []
  let result = '', depth = 0
  const breaks = new Set(['SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'VALUES', 'SET', 'UNION'])
  for (const token of tokens) {
    if (/^\s+$/.test(token)) {if (result && !/\s$/.test(result)) result += ' '; continue}
    const upper = token.toUpperCase(), word = known.has(upper) ? upper : token
    if (token === '(') depth++
    if (token === ')') depth--
    if (breaks.has(upper) && depth === 0 && result.trim()) result = result.trimEnd() + '\n'
    result += word
    if (token === ';') result += '\n\n'
    if (token.startsWith('--') && !token.endsWith('\n')) result += '\n'
  }
  return result.trim()
}

export function quoteName(name: string): string {return '`' + name.replaceAll('`', '``') + '`'}

export function positionOffset(sql: string, line = 1, column = 1): number {
  const lines = sql.split('\n')
  const prefix = lines.slice(0, Math.max(0, line - 1)).reduce((sum, value) => sum + value.length + 1, 0)
  // 后端以 Unicode 码点计列，CodeMirror 使用 UTF-16 偏移。
  return Math.min(sql.length, prefix + [...(lines[line - 1] ?? '')].slice(0, column - 1).join('').length)
}

export function csv(columns: string[], rows: unknown[][]): string {
  const cell = (value: unknown) => {
    let text = value === null ? '' : String(value)
    // WHY：CSV 常被 Excel 打开，阻止数据库内容触发公式执行。
    if (/^[=+\-@\t\r]/.test(text)) text = "'" + text
    return '"' + text.replaceAll('"', '""') + '"'
  }
  return '\uFEFF' + [columns, ...rows].map(row => row.map(cell).join(',')).join('\r\n')
}
