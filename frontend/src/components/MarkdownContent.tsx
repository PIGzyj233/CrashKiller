import { Fragment, type ReactNode } from "react";

export interface MarkdownContentProps {
  content: string;
  className?: string;
}

const SAFE_URL_RE = /^https?:\/\/[^\s<>]+$/i;

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  // Tokenize inline code first so C++ symbols like foo_bar<T> inside backticks
  // are never touched by emphasis/link matchers.
  const nodes: ReactNode[] = [];
  const tokenRe =
    /`([^`\n]+)`|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*\n]+?)\*\*|~~([^~\n]+?)~~|(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)/g;

  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let tokenIdx = 0;

  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    const key = `${keyPrefix}-tok-${tokenIdx++}`;
    if (match[1] !== undefined) {
      nodes.push(
        <code
          key={key}
          className="break-all rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[12px] text-slate-800 ring-1 ring-inset ring-slate-200/80"
        >
          {match[1]}
        </code>,
      );
    } else if (match[2] !== undefined && match[3] !== undefined) {
      const href = match[3].trim();
      if (SAFE_URL_RE.test(href)) {
        nodes.push(
          <a
            key={key}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-indigo-600 underline decoration-indigo-300 underline-offset-2 hover:text-indigo-700"
          >
            {renderInlineMarkdown(match[2], `${key}-a`)}
          </a>,
        );
      } else {
        nodes.push(match[0]);
      }
    } else if (match[4] !== undefined) {
      nodes.push(
        <strong key={key} className="font-semibold text-slate-900">
          {renderInlineMarkdown(match[4], `${key}-b`)}
        </strong>,
      );
    } else if (match[5] !== undefined) {
      nodes.push(
        <del key={key} className="text-slate-500 line-through">
          {renderInlineMarkdown(match[5], `${key}-s`)}
        </del>,
      );
    } else if (match[6] !== undefined) {
      nodes.push(
        <em key={key} className="italic">
          {renderInlineMarkdown(match[6], `${key}-i`)}
        </em>,
      );
    }

    lastIndex = tokenRe.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

export function InlineMarkdown({
  content,
  className,
}: MarkdownContentProps) {
  return <span className={className}>{renderInlineMarkdown(content, "inl")}</span>;
}

function renderMultilineInline(lines: string[], keyPrefix: string): ReactNode {
  return lines.map((line, idx) => (
    <Fragment key={`${keyPrefix}-ln-${idx}`}>
      {idx > 0 && <br />}
      {renderInlineMarkdown(line, `${keyPrefix}-ln-${idx}`)}
    </Fragment>
  ));
}

function isHorizontalRule(line: string): boolean {
  return /^\s*(?:[-*_]\s*){3,}$/.test(line);
}

function parseHeading(line: string): { level: number; text: string } | null {
  const m = /^\s*(#{1,4})\s+(.+?)\s*#*$/.exec(line);
  if (!m) {
    return null;
  }
  return { level: m[1].length, text: m[2].trim() };
}

function parseUnorderedItem(line: string): { indent: number; text: string } | null {
  const m = /^(\s*)(?:[-*+]|•)\s+(.*)$/.exec(line);
  if (!m) {
    return null;
  }
  return { indent: m[1].length, text: m[2] };
}

function parseOrderedItem(
  line: string,
): { indent: number; num: number; text: string } | null {
  const m = /^(\s*)(\d+)[.)]\s+(.*)$/.exec(line);
  if (!m) {
    return null;
  }
  return { indent: m[1].length, num: Number(m[2]), text: m[3] };
}

function parseTableRow(line: string): string[] | null {
  const trimmed = line.trim();
  if (!trimmed.includes("|")) {
    return null;
  }
  const normalized = trimmed.replace(/^\|/, "").replace(/\|$/, "");
  const cells = normalized.split("|").map((c) => c.trim());
  return cells.length >= 2 ? cells : null;
}

function isTableSeparator(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed.includes("|")) {
    return false;
  }
  const normalized = trimmed.replace(/^\|/, "").replace(/\|$/, "");
  const cells = normalized.split("|").map((c) => c.trim());
  return cells.length >= 2 && cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function isBlockStart(lines: string[], index: number): boolean {
  const line = lines[index];
  if (line.trim() === "") return true;
  if (/^\s*```/.test(line)) return true;
  if (parseHeading(line) !== null) return true;
  if (isHorizontalRule(line)) return true;
  if (/^\s*>\s?/.test(line)) return true;
  if (parseUnorderedItem(line) !== null) return true;
  if (parseOrderedItem(line) !== null) return true;
  if (
    index + 1 < lines.length &&
    parseTableRow(line) !== null &&
    isTableSeparator(lines[index + 1])
  ) {
    return true;
  }
  return false;
}

export function MarkdownContent({ content, className }: MarkdownContentProps) {
  const normalized = content.replace(/\r\n?/g, "\n").trim();
  if (!normalized) {
    return null;
  }

  const lines = normalized.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let blockIdx = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Skip blank lines
    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Fenced code block
    const fenceMatch = /^\s*```([\w.+-]*)\s*$/.exec(line);
    if (fenceMatch) {
      const lang = fenceMatch[1];
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) {
        i += 1;
      }
      blocks.push(
        <div
          key={`blk-${blockIdx++}`}
          className="overflow-x-auto rounded-md border border-slate-200 bg-slate-900 text-slate-100"
        >
          {lang && (
            <div className="border-b border-slate-800 px-2.5 py-1 font-mono text-[10px] uppercase tracking-wide text-slate-400">
              {lang}
            </div>
          )}
          <pre className="p-2.5 font-mono text-[11.5px] leading-relaxed">
            <code>{codeLines.join("\n")}</code>
          </pre>
        </div>,
      );
      continue;
    }

    // Heading
    const heading = parseHeading(line);
    if (heading) {
      const key = `blk-${blockIdx++}`;
      const headingClass =
        heading.level <= 2
          ? "mt-5 first:mt-0 mb-1.5 flex items-center gap-2 text-[13.5px] font-semibold text-slate-900"
          : "mt-4 first:mt-0 mb-1.5 flex items-center gap-2 text-[13px] font-semibold text-slate-900";
      blocks.push(
        <h4 key={key} className={headingClass}>
          <span
            aria-hidden="true"
            className="h-3.5 w-1 shrink-0 rounded-full bg-indigo-500/80"
          />
          <span>{renderInlineMarkdown(heading.text, key)}</span>
        </h4>,
      );
      i += 1;
      continue;
    }

    // Horizontal rule
    if (isHorizontalRule(line)) {
      blocks.push(
        <hr key={`blk-${blockIdx++}`} className="my-2.5 border-slate-200" />,
      );
      i += 1;
      continue;
    }

    // Markdown table
    const headerCells = parseTableRow(line);
    if (headerCells && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const key = `blk-${blockIdx++}`;
      i += 2;
      const bodyRows: string[][] = [];
      while (i < lines.length && lines[i].trim() !== "") {
        const rowCells = parseTableRow(lines[i]);
        if (!rowCells) {
          break;
        }
        bodyRows.push(rowCells);
        i += 1;
      }
      blocks.push(
        <div
          key={key}
          className="overflow-x-auto rounded-md border border-slate-200 bg-white"
        >
          <table className="min-w-full divide-y divide-slate-200 text-left text-xs">
            <thead className="bg-slate-50">
              <tr>
                {headerCells.map((cell, cIdx) => (
                  <th
                    key={`${key}-th-${cIdx}`}
                    className="px-2.5 py-1.5 font-semibold text-slate-700"
                  >
                    {renderInlineMarkdown(cell, `${key}-th-${cIdx}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {bodyRows.map((row, rIdx) => (
                <tr key={`${key}-tr-${rIdx}`}>
                  {headerCells.map((_, cIdx) => (
                    <td
                      key={`${key}-td-${rIdx}-${cIdx}`}
                      className="px-2.5 py-1.5 text-slate-700"
                    >
                      {renderInlineMarkdown(
                        row[cIdx] ?? "",
                        `${key}-td-${rIdx}-${cIdx}`,
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // Blockquote
    if (/^\s*>\s?/.test(line)) {
      const key = `blk-${blockIdx++}`;
      const quoteLines: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push(
        <blockquote
          key={key}
          className="border-l-2 border-indigo-300 bg-indigo-50/40 py-1 pl-3 pr-2 text-slate-700"
        >
          {renderMultilineInline(quoteLines, key)}
        </blockquote>,
      );
      continue;
    }

    // Unordered list
    if (parseUnorderedItem(line) !== null) {
      const key = `blk-${blockIdx++}`;
      const items: Array<{ lines: string[]; nested: boolean }> = [];
      const baseIndent = parseUnorderedItem(line)!.indent;
      while (i < lines.length) {
        const parsed = parseUnorderedItem(lines[i]);
        if (parsed !== null) {
          items.push({
            lines: [parsed.text],
            nested: parsed.indent >= baseIndent + 2,
          });
          i += 1;
          while (
            i < lines.length &&
            lines[i].trim() !== "" &&
            !isBlockStart(lines, i)
          ) {
            items[items.length - 1].lines.push(lines[i].trim());
            i += 1;
          }
        } else {
          break;
        }
      }
      blocks.push(
        <ul key={key} className="space-y-1.5 pl-4 list-disc marker:text-slate-400">
          {items.map((item, itemIdx) => (
            <li
              key={`${key}-li-${itemIdx}`}
              className={`leading-relaxed ${item.nested ? "ml-4 list-[circle]" : ""}`}
            >
              {renderMultilineInline(item.lines, `${key}-li-${itemIdx}`)}
            </li>
          ))}
        </ul>,
      );
      continue;
    }

    // Ordered list
    const firstOrdered = parseOrderedItem(line);
    if (firstOrdered !== null) {
      const key = `blk-${blockIdx++}`;
      const startNum = firstOrdered.num;
      const items: Array<{ lines: string[] }> = [];
      while (i < lines.length) {
        const parsed = parseOrderedItem(lines[i]);
        if (parsed !== null) {
          items.push({ lines: [parsed.text] });
          i += 1;
          while (
            i < lines.length &&
            lines[i].trim() !== "" &&
            !isBlockStart(lines, i)
          ) {
            items[items.length - 1].lines.push(lines[i].trim());
            i += 1;
          }
        } else {
          break;
        }
      }
      blocks.push(
        <ol
          key={key}
          start={startNum > 1 ? startNum : undefined}
          className="space-y-1.5 pl-5 list-decimal marker:font-medium marker:text-slate-500"
        >
          {items.map((item, itemIdx) => (
            <li key={`${key}-oli-${itemIdx}`} className="leading-relaxed">
              {renderMultilineInline(item.lines, `${key}-oli-${itemIdx}`)}
            </li>
          ))}
        </ol>,
      );
      continue;
    }

    // Paragraph (consume consecutive plain lines until blank line or next block)
    const key = `blk-${blockIdx++}`;
    const paraLines: string[] = [line];
    i += 1;
    while (i < lines.length && !isBlockStart(lines, i)) {
      paraLines.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={key} className="leading-relaxed">
        {renderMultilineInline(paraLines, key)}
      </p>,
    );
  }

  return (
    <div
      className={`min-w-0 space-y-2.5 break-words text-[13.5px] leading-6 text-slate-700 ${className ?? ""}`}
    >
      {blocks}
    </div>
  );
}

export default MarkdownContent;
