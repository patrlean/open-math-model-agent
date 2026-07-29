import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'

function normalizeMathDelimiters(content: string) {
  // Agent outputs sometimes use LaTeX's \\(...\\) and \\[...\\] delimiters.
  // remark-math understands $...$ / $$...$$, so normalize both conventions.
  const blockMath = /\\\[([\s\S]*?)\\\]/g
  const inlineMath = /\\\(([\s\S]*?)\\\)/g
  return content
    .replace(blockMath, (_, formula: string) => `\n$$\n${formula.trim()}\n$$\n`)
    .replace(inlineMath, (_, formula: string) => `$${formula.trim()}$`)
}

function normalizeInlineHeadings(content: string) {
  // Older verification records joined the preflight sentence and the verifier's
  // first Markdown heading with a space. Restore the required line boundary so
  // those persisted records render correctly as well.
  return content.replace(/([.!?。！？])[\t ]+(#{1,6}[\t ]+)/g, '$1\n\n$2')
}

interface MarkdownContentProps {
  content: string
  className?: string
  normalizeJoinedHeadings?: boolean
}

export function MarkdownContent({
  content,
  className = '',
  normalizeJoinedHeadings = false,
}: MarkdownContentProps) {
  const normalizedContent = normalizeMathDelimiters(
    normalizeJoinedHeadings ? normalizeInlineHeadings(content) : content,
  )

  return (
    <div className={`markdown-content ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          table({ children }) {
            return <div className="markdown-table-scroll"><table>{children}</table></div>
          },
        }}
      >
        {normalizedContent}
      </ReactMarkdown>
    </div>
  )
}
