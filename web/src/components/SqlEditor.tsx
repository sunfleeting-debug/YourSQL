import { useEffect, useRef } from 'react'
import { basicSetup } from 'codemirror'
import { Compartment, EditorState, StateEffect, StateField } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, keymap } from '@codemirror/view'
import { SQLDialect, sql } from '@codemirror/lang-sql'
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language'
import { acceptCompletion, autocompletion, startCompletion, type CompletionContext } from '@codemirror/autocomplete'
import { indentSelection } from '@codemirror/commands'
import { lintGutter, linter, setDiagnostics, setDiagnosticsEffect, type Diagnostic } from '@codemirror/lint'
import { tags } from '@lezer/highlight'
import { api } from '../api'
import { currentStatement, positionOffset, quoteName } from '../sql'
import type { DBError, Dialect, TableMeta } from '../types'

interface Props {
  value: string
  onChange: (value: string) => void
  tables: TableMeta[]
  dialect: Dialect | null
  execute: (all: boolean) => void
  onCursor: (position: number) => void
  onSelection: (from: number, to: number) => void
  error: DBError | null
  running: boolean
  editorRef: React.RefObject<EditorView | null>
}

/** SQL 语义高亮；显式声明以避免 basicSetup 的 fallback 样式在主题变化后失效。 */
const sqlHighlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: '#7b2cbf', fontWeight: '700' },
  { tag: tags.typeName, color: '#075985', fontWeight: '600' },
  { tag: tags.standard(tags.name), color: '#00796b', fontWeight: '600' },
  { tag: tags.name, color: '#243b53' },
  { tag: tags.special(tags.name), color: '#087f78', fontWeight: '600' },
  { tag: tags.string, color: '#b54708' },
  { tag: tags.number, color: '#9a5b00', fontWeight: '600' },
  { tag: tags.bool, color: '#a20f4f', fontWeight: '600' },
  { tag: tags.null, color: '#9f356b', fontStyle: 'italic', fontWeight: '600' },
  { tag: tags.lineComment, color: '#65758b', fontStyle: 'italic' },
  { tag: tags.blockComment, color: '#65758b', fontStyle: 'italic' },
  { tag: tags.operator, color: '#c2410c', fontWeight: '600' },
  { tag: tags.punctuation, color: '#526f83' },
  { tag: tags.paren, color: '#526f83', fontWeight: '600' },
  { tag: tags.brace, color: '#526f83', fontWeight: '600' },
  { tag: tags.squareBracket, color: '#526f83', fontWeight: '600' }
])

/** 返回完整的出错词素，避免只给 SQL 关键字的首字符加下划线。 */
function diagnosticRange(text: string, line?: number, column?: number): { from: number; to: number } | null {
  if (!text.length) return null
  const position = Math.min(text.length, positionOffset(text, line, column))
  const isWordChar = (value: string | undefined): boolean => !!value && /[A-Za-z0-9_$]/.test(value)
  let index = position
  if (index === text.length || !isWordChar(text[index])) {
    while (index > 0 && /\s/.test(text[index - 1])) index -= 1
    if (index > 0 && isWordChar(text[index - 1])) index -= 1
  }
  if (!isWordChar(text[index])) return { from: position, to: position }
  let from = index
  let to = index + 1
  while (from > 0 && isWordChar(text[from - 1])) from -= 1
  while (to < text.length && isWordChar(text[to])) to += 1
  return { from, to }
}

/** Tab 的优先级：接受补全 > 缩进选区 > 打开补全菜单。 */
function handleTab(view: EditorView): boolean {
  if (acceptCompletion(view)) return true
  if (view.state.selection.ranges.some(range => !range.empty)) return indentSelection(view)
  return startCompletion(view)
}

class ErrorLensWidget extends WidgetType {
  constructor(private readonly message: string) {
    super()
  }

  eq(other: ErrorLensWidget): boolean {
    return this.message === other.message
  }

  toDOM(): HTMLElement {
    const element = document.createElement('span')
    element.className = 'cm-errorLens'
    element.setAttribute('role', 'alert')
    element.setAttribute('aria-label', `错误：${this.message}`)
    element.contentEditable = 'false'
    element.textContent = `⚠ ${this.message}`
    return element
  }
}

/** 在每条错误所在行的末尾追加 Error Lens 风格的可读提示。 */
const errorLens = StateField.define({
  create: () => Decoration.none,
  update(decorations, change) {
    const effect = change.effects.find(item => item.is(setDiagnosticsEffect))
    if (!effect) return decorations.map(change.changes)
    const widgets = effect.value
      .filter(diagnostic => diagnostic.severity === 'error')
      .map(diagnostic =>
        Decoration.widget({ widget: new ErrorLensWidget(diagnostic.message), side: 1 }).range(change.state.doc.lineAt(diagnostic.from).to)
      )
    return Decoration.set(widgets, true)
  },
  provide: field => EditorView.decorations.from(field)
})

const activeStatement = StateField.define({
  create: () => Decoration.none,
  update(_value, change) {
    const source = change.state.doc.toString()
    const range = currentStatement(source, change.state.selection.main.head)
    if (!range) return Decoration.none
    const from = change.state.doc.lineAt(range.from).number
    const to = change.state.doc.lineAt(range.to).number
    return Decoration.set(
      Array.from({ length: to - from + 1 }, (_, i) => Decoration.line({ class: 'cm-activeStatement' }).range(change.state.doc.line(from + i).from))
    )
  },
  provide: field => EditorView.decorations.from(field)
})

export default function SqlEditor(props: Props) {
  const container = useRef<HTMLDivElement>(null)
  const live = useRef(props)
  live.current = props
  const language = useRef(new Compartment())
  const editable = useRef(new Compartment())
  useEffect(() => {
    if (!container.current) return
    const complete = (context: CompletionContext) => {
      const word = context.matchBefore(/[\w$]*$/)
      if (!word || (!word.text && !context.explicit)) return null
      const current = live.current
      const unique = new Map<string, { label: string; type: string; detail?: string; apply?: string }>()
      for (const table of current.tables) {
        unique.set(table.name, { label: table.name, type: 'class', detail: '表', apply: quoteName(table.name) })
        for (const column of table.columns)
          unique.set(column.name, {
            label: column.name,
            type: 'property',
            detail: `${table.name} · ${column.type}`,
            apply: quoteName(column.name)
          })
      }
      for (const word of [...(current.dialect?.keywords ?? []), ...(current.dialect?.types ?? []), ...(current.dialect?.functions ?? [])]) {
        unique.set(word, { label: word, type: 'keyword' })
      }
      return { from: word.from, options: [...unique.values()] }
    }
    const view = new EditorView({
      state: EditorState.create({
        doc: live.current.value,
        extensions: [
          basicSetup,
          syntaxHighlighting(sqlHighlightStyle),
          activeStatement,
          errorLens,
          language.current.of(sql()),
          editable.current.of(EditorView.editable.of(true)),
          EditorView.contentAttributes.of({ 'aria-label': 'SQL 编辑器', spellcheck: 'false' }),
          keymap.of([
            { key: 'Tab', run: handleTab },
            {
              key: 'F5',
              run: () => {
                live.current.execute(false)
                return true
              }
            },
            {
              key: 'Shift-F5',
              run: () => {
                live.current.execute(true)
                return true
              }
            }
          ]),
          autocompletion({ override: [complete] }),
          lintGutter(),
          linter(
            async view => {
              const text = view.state.doc.toString()
              if (!text.trim() || text.length > 64000) return []
              try {
                const data = await api<{ diagnostics: DBError[] }>('/api/validate', { sql: text })
                return data.diagnostics.flatMap(error => {
                  const range = diagnosticRange(text, error.line, error.column)
                  return range ? [{ ...range, severity: 'error', message: error.message } as Diagnostic] : []
                })
              } catch {
                return []
              }
            },
            { delay: 650 }
          ),
          EditorView.updateListener.of(update => {
            if (update.docChanged) live.current.onChange(update.state.doc.toString())
            if (update.selectionSet || update.docChanged) {
              live.current.onCursor(update.state.selection.main.head)
              live.current.onSelection(update.state.selection.main.from, update.state.selection.main.to)
            }
          }),
          EditorView.theme({
            '&': { height: '100%', fontSize: '14px' },
            '.cm-scroller': { fontFamily: "'Cascadia Code', Consolas, monospace", lineHeight: '1.8' },
            '.cm-content': { padding: '16px 0' },
            '.cm-gutters': { background: '#fff', border: 'none', color: '#94a1b3', paddingRight: '12px' },
            '.cm-line': { paddingLeft: '12px' },
            '.cm-activeLineGutter': { background: '#e7f4f2', color: '#087f78' },
            '.cm-activeLine': { background: 'transparent' },
            '.cm-cursor': { borderLeftColor: '#087f78' },
            '&.cm-focused': { outline: 'none' },
            // HOW：用左侧内阴影提示当前语句，避免整行背景盖住文本选区。
            '.cm-activeStatement': { background: 'transparent', boxShadow: 'inset 2px 0 #b9ddd8' },
            '.cm-errorLens': {
              display: 'inline-block',
              color: '#b03b42',
              background: '#fff5f5',
              border: '1px solid #f0d4d7',
              borderRadius: '3px',
              padding: '1px 6px',
              marginLeft: '10px',
              fontSize: '11px',
              lineHeight: '1.5',
              fontFamily: "Inter, 'Microsoft YaHei', sans-serif",
              whiteSpace: 'normal',
              overflowWrap: 'anywhere',
              verticalAlign: 'middle'
            },
            '.cm-tooltip': { border: '1px solid #d9e1e8', background: '#fff', borderRadius: '4px' }
          })
        ]
      }),
      parent: container.current
    })
    live.current.editorRef.current = view
    return () => {
      view.destroy()
      live.current.editorRef.current = null
    }
  }, [])

  useEffect(() => {
    const view = props.editorRef.current
    if (view && view.state.doc.toString() !== props.value) view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: props.value } })
  }, [props.value, props.editorRef])
  useEffect(() => {
    const dialect = props.dialect
    if (dialect)
      props.editorRef.current?.dispatch({
        effects: language.current.reconfigure(
          sql({
            dialect: SQLDialect.define({
              // WHY：CodeMirror 会把未加引号的词转成小写后再查 dialect.words；API 返回的大写词表必须先归一化。
              keywords: dialect.keywords.map(word => word.toLowerCase()).join(' '),
              types: dialect.types.map(word => word.toLowerCase()).join(' '),
              builtin: dialect.functions.map(word => word.toLowerCase()).join(' '),
              doubleQuotedStrings: false,
              backslashEscapes: true,
              identifierQuotes: '`"'
            })
          })
        )
      })
  }, [props.dialect, props.editorRef])
  useEffect(() => {
    props.editorRef.current?.dispatch({ effects: editable.current.reconfigure(EditorView.editable.of(!props.running)) })
  }, [props.running, props.editorRef])
  useEffect(() => {
    const view = props.editorRef.current,
      error = props.error
    if (!view || !error) return
    const text = view.state.doc.toString()
    const range = diagnosticRange(text, error.line, error.column)
    if (!range) return
    view.dispatch(setDiagnostics(view.state, [{ ...range, severity: 'error', message: error.message }]))
    view.dispatch({
      selection: { anchor: range.from },
      effects: EditorView.scrollIntoView(range.from, { y: 'center' }) as StateEffect<unknown>
    })
    view.focus()
  }, [props.error, props.editorRef])
  return <div className="editor-host" ref={container} />
}
