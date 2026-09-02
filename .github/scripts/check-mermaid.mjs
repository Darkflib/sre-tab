#!/usr/bin/env node
// Parse every ```mermaid block in the tracked Markdown, and fail on any the
// renderer would reject.
//
// ARCHITECTURE.md is nine diagrams and no prose could replace them, which
// makes them the first thing in this repository that can rot into a visible
// error message rather than into wrong words. GitHub renders an unparseable
// block as a red box reading "Unable to render rich display", in place of the
// diagram, on the page a newcomer is most likely to open first — and nothing
// else here would notice. A reviewer will not either: the diff shows the
// source, and Mermaid source that looks reasonable and is rejected by the
// grammar looks exactly like Mermaid source that is not.
//
// **This checks that the grammar accepts the source. It does not check that
// the picture is any good, and that distinction cost four rewrites the day
// these diagrams were written.** All nine parsed on the first attempt; four
// of them were still wrong when rendered and looked at. The worst had dagre
// routing an edge *through* an unrelated node, so the picture showed an arrow
// between two services that never talk to each other — valid source, false
// diagram. There is no gate for that. Render the thing and look at it:
//
//     npx --yes @mermaid-js/mermaid-cli -i diagram.mmd -o diagram.png
//
// Usage:
//
//     node .github/scripts/check-mermaid.mjs
//
// Needs `mermaid` and `happy-dom` resolvable — see the Docs workflow, which
// installs both at pinned versions. Exits non-zero listing every bad block
// rather than stopping at the first.
//
// **The pinned version is not GitHub's.** GitHub does not publish the Mermaid
// version it renders with and updates it on its own schedule, so this agrees
// with GitHub's grammar only approximately: a diagram using syntax newer than
// whatever GitHub is running would pass here and still render as that red
// box. To find out what GitHub is actually running, put a fenced block
// containing the single word `info` in any Markdown on github.com and look at
// what it renders — that is the only published way to ask.

import { execFileSync } from 'node:child_process'
import fs from 'node:fs'

// The fence rules are CommonMark's, and they are the same ones
// check-doc-links.py implements for the same corpus: up to three spaces of
// indent (four makes it an indented code block instead), three or more
// backticks or tildes, and a close only on the same character, at the same
// length or longer, and carrying no info string. Those rules are what let a
// ````-delimited block hold ``` examples, as CONTRIBUTING.md's does — and
// they are why a ```mermaid found *inside* another fence is content rather
// than a diagram. A document explaining this convention by example must not
// have its example checked.
const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/

function trackedMarkdown() {
  return execFileSync('git', ['ls-files', '-z', '*.md'], { encoding: 'utf8' })
    .split('\0')
    .filter(Boolean)
}

// Returns { file, line, source } for each mermaid block, `line` being the
// opening fence — which is what the reader needs to find it, and one line
// off from where the parser will report its own error.
function mermaidBlocks(file, text) {
  const found = []
  const lines = text.split('\n')
  let marker = null
  let start = 0
  let body = []
  let collecting = false

  for (let i = 0; i < lines.length; i++) {
    const match = FENCE.exec(lines[i])
    if (match) {
      const [, fence, info] = match
      if (marker === null) {
        // A backtick fence may not carry a backtick in its info string.
        if (fence[0] === '`' && info.includes('`')) continue
        marker = fence
        start = i + 1
        body = []
        collecting = info.trim().split(/\s+/)[0] === 'mermaid'
        continue
      }
      // A closing fence carries no info string — CommonMark is explicit, and
      // the difference is a silent pass rather than a nicety. Without the
      // first condition a line like ```markdown inside a ```-opened block
      // reads as a close, which desynchronises every fence after it: a real
      // ```mermaid further down then pairs as a *closing* delimiter and its
      // contents are never checked. Probed on a document holding a plainly
      // invalid diagram, which this gate reported as green.
      if (info.trim() === '' && fence[0] === marker[0] && fence.length >= marker.length) {
        if (collecting) found.push({ file, line: start, source: body.join('\n') })
        marker = null
        collecting = false
      } else if (collecting) {
        body.push(lines[i])
      }
      continue
    }
    if (collecting) body.push(lines[i])
  }
  return found
}

// Mermaid needs a DOM to parse: DOMPurify is loaded at parse time and throws
// `DOMPurify.addHook is not a function` without one. Discovered by running it
// without — where two of the nine diagrams parsed anyway, so an absent DOM
// does not fail cleanly, it fails *partially*. happy-dom rather than jsdom
// because frontend/package.json already pins happy-dom for the two Vitest
// files that need a document, and one DOM implementation in a repository is
// enough.
async function loadMermaid() {
  const { Window } = await import('happy-dom')
  const window = new Window({ url: 'https://localhost/' })
  const globals = [
    'document', 'Element', 'SVGElement', 'HTMLElement', 'Node', 'DOMParser',
    'XMLSerializer', 'getComputedStyle', 'requestAnimationFrame',
    'cancelAnimationFrame', 'MutationObserver', 'DocumentFragment',
  ]
  globalThis.window = window
  globalThis.self = window
  for (const name of globals) globalThis[name] = window[name]

  const mermaid = (await import('mermaid')).default
  mermaid.initialize({ startOnLoad: false })
  return mermaid
}

// Prove the parser rejects things *in this environment* before believing that
// it accepted anything. Two questions, both from AGENTS.md: what does this
// check say when its subject is missing, and would it still pass if what it
// protects were reverted?
//
// The second is what this answers. A parser that throws on everything, or one
// wired up so that nothing reaches it, are both indistinguishable from a
// corpus of nine good diagrams — the run prints the same line either way. So
// a known-good diagram must parse and a known-bad one must not, and if either
// disagrees this exits 2 without reporting on the corpus at all, because at
// that point it has nothing to say about it.
const GOOD = 'flowchart TB\n    a["one"] --> b["two"]\n'
const BAD = 'flowchart TB\n    a["never closed --> b\n'

async function selfTest(mermaid) {
  try {
    await mermaid.parse(GOOD)
  } catch (error) {
    return `the parser rejected a known-good diagram: ${error?.message ?? error}`
  }
  try {
    await mermaid.parse(BAD)
  } catch {
    return null
  }
  return 'the parser accepted a known-bad diagram, so a green run means nothing'
}

async function main() {
  const mermaid = await loadMermaid()

  const broken = await selfTest(mermaid)
  if (broken) {
    process.stderr.write(`check-mermaid: self-test failed — ${broken}\n`)
    return 2
  }

  const documents = trackedMarkdown()
  const blocks = documents.flatMap((file) =>
    mermaidBlocks(file, fs.readFileSync(file, 'utf8')),
  )

  // Zero blocks is the silent-pass direction: this script cannot tell a
  // repository that has no diagrams from an extractor that has stopped
  // finding them, and the two print the same success line. Since there are
  // diagrams, finding none is a fault — and if they are ever all deleted,
  // deleting this check is the honest response to it going red.
  if (blocks.length === 0) {
    process.stderr.write(
      'check-mermaid: no ```mermaid blocks found in ' +
        `${documents.length} tracked documents. Either every diagram was ` +
        'removed — in which case remove this check too — or the extractor ' +
        'is broken.\n',
    )
    return 1
  }

  const problems = []
  for (const block of blocks) {
    try {
      await mermaid.parse(block.source)
    } catch (error) {
      const detail = String(error?.message ?? error).replace(/\n/g, '\n    ')
      problems.push(`${block.file}:${block.line}: ${detail}`)
    }
  }

  if (problems.length > 0) {
    for (const problem of problems) process.stderr.write(`${problem}\n`)
    process.stderr.write(`\n${problems.length} problem(s).\n`)
    return 1
  }

  process.stdout.write(
    `${blocks.length} mermaid diagrams in ${documents.length} documents, all parse.\n`,
  )
  return 0
}

process.exit(await main())
