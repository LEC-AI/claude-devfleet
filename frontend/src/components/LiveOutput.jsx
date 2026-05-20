import React, { useRef, useEffect, useState, useCallback, useMemo } from 'react';

/* ────────────────────────────────────────────────────────────
   Inline styles — self-contained, no external CSS dependencies
   beyond the existing CSS-variable palette.
   ──────────────────────────────────────────────────────────── */

const S = {
  wrapper: {
    background: '#0d1117',
    border: '1px solid #1e2a3a',
    borderRadius: 10,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', Consolas, monospace",
    boxShadow: '0 8px 32px rgba(0,0,0,0.45), 0 2px 8px rgba(0,0,0,0.3)',
    position: 'relative',
  },

  /* ── Chrome bar ── */
  chrome: {
    display: 'flex',
    alignItems: 'center',
    padding: '10px 16px',
    background: '#161b22',
    borderBottom: '1px solid #1e2a3a',
    gap: 10,
    userSelect: 'none',
    minHeight: 42,
  },
  dots: { display: 'flex', gap: 7 },
  dot: (c) => ({
    width: 12, height: 12, borderRadius: '50%',
    background: c,
    boxShadow: `0 0 4px ${c}55`,
  }),
  chromeTitle: {
    flex: 1,
    textAlign: 'center',
    fontSize: 12,
    fontWeight: 600,
    color: '#8b949e',
    letterSpacing: '0.04em',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  copyBtn: {
    background: 'rgba(139,148,158,0.1)',
    border: '1px solid rgba(139,148,158,0.2)',
    borderRadius: 6,
    color: '#8b949e',
    fontSize: 11,
    fontWeight: 600,
    padding: '4px 10px',
    cursor: 'pointer',
    transition: 'all .15s',
    fontFamily: 'inherit',
    display: 'flex',
    alignItems: 'center',
    gap: 5,
  },

  /* ── Output area ── */
  output: {
    flex: 1,
    overflowY: 'auto',
    maxHeight: '70vh',
    minHeight: 200,
    padding: '8px 0',
    scrollBehavior: 'smooth',
    position: 'relative',
  },

  /* ── Row (line) ── */
  row: {
    display: 'flex',
    minHeight: 22,
    lineHeight: '22px',
    fontSize: 13,
  },
  gutter: {
    width: 52,
    minWidth: 52,
    textAlign: 'right',
    paddingRight: 14,
    color: '#3b4654',
    fontSize: 12,
    userSelect: 'none',
    lineHeight: '22px',
    flexShrink: 0,
  },
  lineContent: {
    flex: 1,
    paddingRight: 16,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },

  /* ── Tool call header ── */
  toolHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '6px 12px',
    margin: '4px 0 0 52px',
    background: 'rgba(96,165,250,0.07)',
    borderRadius: '6px 6px 0 0',
    border: '1px solid rgba(96,165,250,0.15)',
    borderBottom: 'none',
    cursor: 'pointer',
    userSelect: 'none',
    color: '#79c0ff',
    fontWeight: 600,
    fontSize: 12,
    transition: 'background .15s',
  },
  toolIcon: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 22, height: 22,
    borderRadius: 5,
    background: 'rgba(96,165,250,0.15)',
    fontSize: 12,
    flexShrink: 0,
  },
  toolChevron: (open) => ({
    marginLeft: 'auto',
    fontSize: 10,
    transition: 'transform .2s',
    transform: open ? 'rotate(90deg)' : 'rotate(0)',
    color: '#4d6a8a',
  }),
  toolBody: (open) => ({
    margin: '0 0 4px 52px',
    padding: open ? '8px 12px' : 0,
    maxHeight: open ? 300 : 0,
    overflow: open ? 'auto' : 'hidden',
    background: 'rgba(96,165,250,0.03)',
    borderRadius: '0 0 6px 6px',
    border: open ? '1px solid rgba(96,165,250,0.15)' : '1px solid transparent',
    borderTop: 'none',
    transition: 'max-height .25s ease, padding .25s ease',
    fontSize: 13,
    color: '#c9d1d9',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  }),

  /* ── Tool result ── */
  toolResultWrap: {
    margin: '0 0 6px 52px',
    padding: '6px 12px',
    background: 'rgba(139,148,158,0.04)',
    borderLeft: '2px solid #2d3748',
    borderRadius: '0 6px 6px 0',
    maxHeight: 220,
    overflowY: 'auto',
    fontSize: 12,
    lineHeight: '20px',
    color: '#6e7a88',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },

  /* ── Footer / usage bar ── */
  footer: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 16px',
    background: '#161b22',
    borderTop: '1px solid #1e2a3a',
    fontSize: 11,
    color: '#8b949e',
    gap: 16,
    flexWrap: 'wrap',
    minHeight: 36,
  },
  footerStat: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
  },
  footerLabel: { color: '#4d6a8a', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' },
  footerValue: { color: '#58a6ff' },

  /* ── Typing indicator ── */
  typingWrap: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    padding: '4px 0 4px 52px',
    height: 28,
  },
  cursor: {
    display: 'inline-block',
    width: 8,
    height: 17,
    background: '#58a6ff',
    borderRadius: 1,
    animation: 'lo-blink 1s step-end infinite',
  },

  /* ── Floating scroll-to-bottom ── */
  fab: {
    position: 'absolute',
    bottom: 14,
    right: 20,
    width: 36, height: 36,
    borderRadius: '50%',
    background: '#1f6feb',
    border: 'none',
    color: '#fff',
    fontSize: 18,
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    boxShadow: '0 4px 12px rgba(31,111,235,0.4)',
    transition: 'opacity .2s, transform .2s',
    zIndex: 5,
  },

  /* ── Auto-scroll toggle ── */
  autoScrollBtn: (active) => ({
    background: active ? 'rgba(88,166,255,0.12)' : 'rgba(139,148,158,0.08)',
    border: `1px solid ${active ? 'rgba(88,166,255,0.3)' : 'rgba(139,148,158,0.15)'}`,
    borderRadius: 6,
    color: active ? '#58a6ff' : '#6e7a88',
    fontSize: 11,
    fontWeight: 600,
    padding: '4px 9px',
    cursor: 'pointer',
    fontFamily: 'inherit',
    transition: 'all .15s',
    display: 'flex',
    alignItems: 'center',
    gap: 4,
  }),
};


/* ────────────────────────────────────────────────────────────
   Syntax-style highlighter for plain text lines
   ──────────────────────────────────────────────────────────── */

const TOKEN_RE = /(https?:\/\/[^\s)]+)|(\/[\w./-]{2,})|(\b\d[\d,.]*\b)/g;

function highlightText(raw) {
  if (!raw) return raw;
  const parts = [];
  let last = 0;
  let m;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(raw)) !== null) {
    if (m.index > last) parts.push(raw.slice(last, m.index));
    if (m[1]) {
      // URL
      parts.push(<span key={m.index} style={{ color: '#58a6ff', textDecoration: 'underline', textUnderlineOffset: 2 }}>{m[1]}</span>);
    } else if (m[2]) {
      // File path
      parts.push(<span key={m.index} style={{ color: '#7ee787' }}>{m[2]}</span>);
    } else if (m[3]) {
      // Number
      parts.push(<span key={m.index} style={{ color: '#d2a8ff' }}>{m[3]}</span>);
    }
    last = m.index + m[0].length;
  }
  if (last < raw.length) parts.push(raw.slice(last));
  return parts.length ? parts : raw;
}


/* ────────────────────────────────────────────────────────────
   Tool-name extraction helper
   ──────────────────────────────────────────────────────────── */

const TOOL_ICONS = {
  Read: '\u{1F4C4}',
  Edit: '\u270F\uFE0F',
  Write: '\u{1F4DD}',
  Bash: '\u{1F4BB}',
  Grep: '\u{1F50D}',
  Glob: '\u{1F4C1}',
  default: '\u2699\uFE0F',
};

function parseToolName(text) {
  // Try to extract tool name from first line, e.g. "Tool: Read (/path...)"
  const m = text?.match(/^(?:Tool:\s*)?(\w+)[\s(]/);
  return m ? m[1] : null;
}

function iconFor(name) {
  return TOOL_ICONS[name] || TOOL_ICONS.default;
}


/* ────────────────────────────────────────────────────────────
   Collapsible tool block
   ──────────────────────────────────────────────────────────── */

function ToolBlock({ text }) {
  const [open, setOpen] = useState(true);
  const name = parseToolName(text) || 'Tool';
  return (
    <>
      <div
        style={S.toolHeader}
        onClick={() => setOpen(!open)}
        onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(96,165,250,0.12)'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(96,165,250,0.07)'; }}
      >
        <span style={S.toolIcon}>{iconFor(name)}</span>
        <span>{name}</span>
        <span style={S.toolChevron(open)}>&#9654;</span>
      </div>
      <div style={S.toolBody(open)}>
        {open && highlightText(text)}
      </div>
    </>
  );
}


/* ────────────────────────────────────────────────────────────
   Usage footer data extraction
   ──────────────────────────────────────────────────────────── */

function parseUsage(text) {
  if (!text) return null;
  const tokens = {};
  const costM = text.match(/\$[\d.]+/);
  if (costM) tokens.cost = costM[0];
  const inM = text.match(/(\d[\d,]*)\s*input/i);
  if (inM) tokens.input = inM[1];
  const outM = text.match(/(\d[\d,]*)\s*output/i);
  if (outM) tokens.output = outM[1];
  return Object.keys(tokens).length ? tokens : { raw: text };
}


/* ────────────────────────────────────────────────────────────
   Keyframes injected once
   ──────────────────────────────────────────────────────────── */

let injected = false;
function injectKeyframes() {
  if (injected) return;
  injected = true;
  const sheet = document.createElement('style');
  sheet.textContent = `
    @keyframes lo-blink { 0%,100%{opacity:1} 50%{opacity:0} }
    .lo-output::-webkit-scrollbar { width:6px }
    .lo-output::-webkit-scrollbar-track { background:transparent }
    .lo-output::-webkit-scrollbar-thumb { background:#2a3444; border-radius:3px }
    .lo-output::-webkit-scrollbar-thumb:hover { background:#3b4f66 }
    .lo-tool-result::-webkit-scrollbar { width:4px }
    .lo-tool-result::-webkit-scrollbar-track { background:transparent }
    .lo-tool-result::-webkit-scrollbar-thumb { background:#2a3444; border-radius:2px }
  `;
  document.head.appendChild(sheet);
}


/* ────────────────────────────────────────────────────────────
   Main Component
   ──────────────────────────────────────────────────────────── */

export default function LiveOutput({ events, status }) {
  const containerRef = useRef(null);
  const bottomRef = useRef(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [showFab, setShowFab] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => { injectKeyframes(); }, []);

  // Auto-scroll when new events arrive
  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [events, autoScroll]);

  // Track scroll position to show/hide FAB and auto-disable auto-scroll
  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowFab(gap > 150);
    if (gap < 50) {
      setAutoScroll(true);
    } else if (gap > 200) {
      setAutoScroll(false);
    }
  }, []);

  const scrollToBottom = useCallback(() => {
    setAutoScroll(true);
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  const handleCopy = useCallback(() => {
    const text = events.map(e => e.text || '').join('');
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  }, [events]);

  // Extract last usage event for the footer
  const usageEvt = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'usage') return events[i];
    }
    return null;
  }, [events]);

  const usageData = useMemo(() => parseUsage(usageEvt?.text), [usageEvt]);

  // Build renderable rows — assign line numbers to text lines
  let lineNum = 0;

  const statusLabel =
    status === 'running' ? '\u25CF LIVE' :
    status === 'completed' ? '\u2713 COMPLETED' :
    '\u2717 ' + (status || '').toUpperCase();

  const statusColor =
    status === 'running' ? '#3fb950' :
    status === 'completed' ? '#58a6ff' :
    '#f85149';

  return (
    <div style={S.wrapper}>
      {/* ── Chrome bar ── */}
      <div style={S.chrome}>
        <div style={S.dots}>
          <span style={S.dot('#ff5f57')} />
          <span style={S.dot('#febc2e')} />
          <span style={S.dot('#28c840')} />
        </div>
        <div style={S.chromeTitle}>
          <span style={{ color: statusColor, marginRight: 8 }}>{statusLabel}</span>
          <span style={{ color: '#4d6a8a' }}>Agent Terminal</span>
        </div>
        <button
          style={S.autoScrollBtn(autoScroll)}
          onClick={() => setAutoScroll(!autoScroll)}
          title={autoScroll ? 'Auto-scroll ON' : 'Auto-scroll OFF'}
        >
          {autoScroll ? '\u2193 Auto' : '\u2193 Manual'}
        </button>
        <button
          style={{
            ...S.copyBtn,
            ...(copied ? { color: '#3fb950', borderColor: 'rgba(63,185,80,0.3)' } : {}),
          }}
          onClick={handleCopy}
          onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(139,148,158,0.18)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(139,148,158,0.1)'; }}
        >
          {copied ? '\u2713 Copied' : '\u2398 Copy'}
        </button>
      </div>

      {/* ── Output ── */}
      <div
        className="lo-output"
        style={S.output}
        ref={containerRef}
        onScroll={handleScroll}
      >
        {events.map((evt, i) => {
          if (evt.type === 'tool') {
            return <ToolBlock key={i} text={evt.text} />;
          }

          if (evt.type === 'tool_result') {
            return (
              <div key={i} className="lo-tool-result" style={S.toolResultWrap}>
                {highlightText(evt.text)}
              </div>
            );
          }

          if (evt.type === 'usage') {
            // Rendered in footer instead
            return null;
          }

          // Regular text — show line numbers
          const lines = (evt.text || '').split('\n');
          return lines.map((line, li) => {
            lineNum++;
            return (
              <div key={`${i}-${li}`} style={S.row}>
                <span style={S.gutter}>{lineNum}</span>
                <span style={S.lineContent}>{highlightText(line)}</span>
              </div>
            );
          });
        })}

        {/* Typing indicator */}
        {status === 'running' && (
          <div style={S.typingWrap}>
            <span style={S.cursor} />
          </div>
        )}

        <div ref={bottomRef} />

        {/* Scroll-to-bottom FAB */}
        {showFab && (
          <button
            style={S.fab}
            onClick={scrollToBottom}
            title="Scroll to bottom"
          >
            &#8595;
          </button>
        )}
      </div>

      {/* ── Footer / usage bar ── */}
      <div style={S.footer}>
        <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
          {usageData?.cost && (
            <span style={S.footerStat}>
              <span style={S.footerLabel}>Cost</span>
              <span style={S.footerValue}>{usageData.cost}</span>
            </span>
          )}
          {usageData?.input && (
            <span style={S.footerStat}>
              <span style={S.footerLabel}>In</span>
              <span style={S.footerValue}>{usageData.input}</span>
            </span>
          )}
          {usageData?.output && (
            <span style={S.footerStat}>
              <span style={S.footerLabel}>Out</span>
              <span style={S.footerValue}>{usageData.output}</span>
            </span>
          )}
          {usageData?.raw && (
            <span style={{ ...S.footerStat, color: '#8b949e' }}>{usageData.raw}</span>
          )}
          {!usageData && (
            <span style={{ color: '#4d6a8a' }}>
              {status === 'running' ? 'Agent working...' : events.length === 0 ? 'Waiting for output...' : `${lineNum} lines`}
            </span>
          )}
        </div>
        <span style={{ color: '#2d3748', fontSize: 10 }}>Nexis365 DevFleet™</span>
      </div>
    </div>
  );
}
