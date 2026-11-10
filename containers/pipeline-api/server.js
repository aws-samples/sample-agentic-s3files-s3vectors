// InvoiceIQ pipeline-api
//
// A tiny read-mostly backend for the agentic UI. It does ONE thing: look at the
// files the agents leave on the shared Amazon S3 Files mount and report them as
// JSON. The UI never talks to the agents directly — it sees the pipeline purely
// through the files on the mount, which is the whole point of the design.
//
//   GET  /api/pipeline          -> per-stage file counts + listings
//   GET  /api/invoice/:id       -> one invoice's state across every stage
//   GET  /api/file/:stage/:id   -> raw JSON an agent wrote for one stage
//   GET  /api/memory            -> what the embedder has stored (memory/ markers)
//   PUT  /api/upload/:id        -> write a raw PDF body into incoming/ (triggers the pipeline)
//   GET  /healthz
const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
const MOUNT = process.env.S3_FILES_MOUNT || '/mnt/s3files';
const PORT = process.env.PORT || 3000;

// The pipeline stages, in order, each backed by a directory on the mount.
const STAGES = [
  { id: 'incoming', name: 'Incoming', agent: 'S3 Upload', kind: 'pdf' },
  { id: 'tasks', name: 'Orchestrator', agent: 'Orchestrator', kind: 'json' },
  { id: 'classified', name: 'Classifier', agent: 'Classifier', kind: 'json' },
  { id: 'extracted', name: 'Extractor', agent: 'Extractor', kind: 'json' },
  { id: 'validated', name: 'Validator', agent: 'Validator', kind: 'json' },
  { id: 'memory', name: 'Embedder', agent: 'Embedder', kind: 'json' },
];

function safeId(id) {
  // invoice ids are simple slugs; reject anything else (path traversal guard).
  return /^[A-Za-z0-9._-]+$/.test(id) ? id : null;
}

function listDir(dir) {
  const full = path.join(MOUNT, dir);
  try {
    return fs.readdirSync(full)
      .filter((f) => !f.endsWith('.tmp') && (f.endsWith('.json') || f.endsWith('.pdf')))
      .map((f) => {
        const st = fs.statSync(path.join(full, f));
        return { name: f, id: f.replace(/\.(json|pdf)$/, ''), size: st.size, modified: st.mtime };
      })
      .sort((a, b) => a.modified - b.modified);
  } catch {
    return [];
  }
}

function readStageFile(stage, id) {
  const sid = safeId(id);
  if (!sid) return null;
  try {
    const p = path.join(MOUNT, stage, `${sid}.json`);
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch {
    return null;
  }
}

app.get('/healthz', (_req, res) => res.json({ ok: true, mount: MOUNT }));

app.get('/api/pipeline', (_req, res) => {
  const stages = STAGES.map((s) => {
    const files = listDir(s.id);
    return { ...s, count: files.length, files };
  });
  const flagged = listDir('flagged');
  res.json({ stages, flagged: { count: flagged.length, files: flagged }, timestamp: new Date().toISOString() });
});

// Aggregate one invoice across every stage so the UI can render its journey.
app.get('/api/invoice/:id', (req, res) => {
  const id = safeId(req.params.id);
  if (!id) return res.status(400).json({ error: 'invalid id' });
  const stages = {};
  for (const s of STAGES) {
    if (s.id === 'incoming') {
      stages.incoming = fs.existsSync(path.join(MOUNT, 'incoming', `${id}.pdf`));
    } else {
      stages[s.id] = readStageFile(s.id, id);
    }
  }
  stages.flagged = readStageFile('flagged', id);
  res.json({ id, stages });
});

app.get('/api/file/:stage/:id', (req, res) => {
  // Validate the stage path segment against the known stages (path-traversal guard),
  // the same way /api/agent/:stage does. id is guarded by safeId() in readStageFile.
  const known = STAGES.some((s) => s.id === req.params.stage) || req.params.stage === 'flagged';
  if (!known) return res.status(404).json({ error: 'unknown stage' });
  const data = readStageFile(req.params.stage, req.params.id);
  res.json(data || { error: 'not found' });
});

// One agent's recent activity: the files it has written, oldest-to-newest, with
// their parsed contents. The UI renders this as a CLI-style activity stream so
// you can watch what each agent decided — all of it read straight off the mount.
app.get('/api/agent/:stage', (req, res) => {
  const stage = STAGES.find((s) => s.id === req.params.stage);
  if (!stage) return res.status(404).json({ error: 'unknown stage' });
  const limit = Math.min(parseInt(req.query.limit, 10) || 25, 100);
  const files = listDir(stage.id).slice(-limit);
  const items = files.map((f) => ({
    id: f.id,
    modified: f.modified,
    data: stage.kind === 'json' ? readStageFile(stage.id, f.id) : null,
  }));
  res.json({ stage: stage.id, agent: stage.agent, items });
});

app.get('/api/memory', (_req, res) => {
  const files = listDir('memory');
  const items = files
    .map((f) => readStageFile('memory', f.id))
    .filter(Boolean);
  res.json({ count: items.length, items });
});

// Upload a PDF straight onto the mount -> S3 Files syncs it to s3://.../incoming/,
// EventBridge fires, SQS delivers, the orchestrator picks it up. Raw body (no multer).
app.put('/api/upload/:id', express.raw({ type: '*/*', limit: '15mb' }), (req, res) => {
  const id = safeId(req.params.id);
  if (!id) return res.status(400).json({ error: 'invalid id' });
  try {
    fs.mkdirSync(path.join(MOUNT, 'incoming'), { recursive: true });
    const dest = path.join(MOUNT, 'incoming', `${id}.pdf`);
    fs.writeFileSync(dest, req.body);
    res.json({ ok: true, wrote: `incoming/${id}.pdf`, bytes: req.body.length });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.listen(PORT, '0.0.0.0', () => console.log(`InvoiceIQ pipeline-api on :${PORT}, mount ${MOUNT}`));
