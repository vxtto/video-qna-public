const picker = document.getElementById("video-picker");
const player = document.getElementById("player");
const segmentsEl = document.getElementById("segments");
const progressEl = document.getElementById("progress");
const movieTitleEl = document.getElementById("movie-title");

let currentVideo = null;
let segmentEls = new Map(); // segment id -> <li>

function fmt(ms) {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function loadVideoList() {
  const videos = await fetch("/api/videos").then((r) => r.json());
  picker.innerHTML = "";
  if (!videos.length) {
    movieTitleEl.textContent = "No movies available";
    return;
  }
  for (const v of videos) {
    const opt = document.createElement("option");
    opt.value = v.slug;
    opt.textContent = `${v.title} (${v.reviewed_count}/${v.segment_count} reviewed)`;
    picker.appendChild(opt);
  }
  await loadVideo(videos[0].slug);
}

async function loadVideo(slug) {
  const video = await fetch(`/api/videos/${slug}`).then((r) => r.json());
  currentVideo = video;
  picker.value = video.slug;
  movieTitleEl.textContent = video.title;
  player.src = `/media/${video.filename}`;
  renderSegments(video.segments);
}

function renderSegments(segments) {
  segmentsEl.innerHTML = "";
  segmentEls = new Map();
  for (const seg of segments) {
    const li = document.createElement("li");
    li.className = `segment status-${seg.review_status}`;
    li.dataset.start = seg.start_ms;
    li.dataset.end = seg.end_ms;

    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = fmt(seg.start_ms);

    const text = document.createElement("span");
    text.className = "text";
    text.textContent = seg.text || "(no speech detected)";

    const review = document.createElement("span");
    review.className = "review";
    const correctBtn = document.createElement("button");
    correctBtn.textContent = "✓";
    correctBtn.className = seg.review_status === "correct" ? "on correct" : "";
    const incorrectBtn = document.createElement("button");
    incorrectBtn.textContent = "✗";
    incorrectBtn.className = seg.review_status === "incorrect" ? "on incorrect" : "";

    correctBtn.onclick = (e) => {
      e.stopPropagation();
      setReview(seg.id, li, "correct");
    };
    incorrectBtn.onclick = (e) => {
      e.stopPropagation();
      setReview(seg.id, li, "incorrect");
    };

    review.append(correctBtn, incorrectBtn);
    li.append(ts, text, review);

    if (seg.review_status === "incorrect") {
      li.appendChild(correctionBox(seg));
    }

    li.onclick = () => {
      player.currentTime = seg.start_ms / 1000;
      player.play();
    };

    segmentsEl.appendChild(li);
    segmentEls.set(seg.id, li);
  }
}

function correctionBox(seg) {
  const box = document.createElement("textarea");
  box.rows = 2;
  box.placeholder = "corrected transcript text...";
  box.value = seg.corrected_text || "";
  box.onclick = (e) => e.stopPropagation();
  box.onblur = () => patchSegment(seg.id, { review_status: "incorrect", corrected_text: box.value });
  return box;
}

async function setReview(id, li, status) {
  // toggle off if clicking the already-active state
  const current = [...li.classList].find((c) => c.startsWith("status-"))?.replace("status-", "");
  const next = current === status ? "unreviewed" : status;
  const updated = await patchSegment(id, { review_status: next });

  li.className = `segment status-${updated.review_status}`;
  const existingBox = li.querySelector("textarea");
  if (existingBox) existingBox.remove();
  if (updated.review_status === "incorrect") {
    li.appendChild(correctionBox(updated));
  }
  renderReviewButtons(li, id, updated.review_status);
}

function renderReviewButtons(li, id, status) {
  const review = li.querySelector(".review");
  review.innerHTML = "";
  const correctBtn = document.createElement("button");
  correctBtn.textContent = "✓";
  correctBtn.className = status === "correct" ? "on correct" : "";
  correctBtn.onclick = (e) => { e.stopPropagation(); setReview(id, li, "correct"); };
  const incorrectBtn = document.createElement("button");
  incorrectBtn.textContent = "✗";
  incorrectBtn.className = status === "incorrect" ? "on incorrect" : "";
  incorrectBtn.onclick = (e) => { e.stopPropagation(); setReview(id, li, "incorrect"); };
  review.append(correctBtn, incorrectBtn);
}

async function patchSegment(id, body) {
  const res = await fetch(`/api/segments/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

player.addEventListener("timeupdate", () => {
  const ms = player.currentTime * 1000;
  progressEl.textContent = fmt(ms);
  for (const [, li] of segmentEls) {
    const active = ms >= +li.dataset.start && ms < +li.dataset.end;
    li.classList.toggle("active", active);
    if (active) li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
});

picker.addEventListener("change", () => loadVideo(picker.value));

loadVideoList();

// --- agent test console -----------------------------------------------
// Bare-bones chat UI against POST /api/chat/stream (Server-Sent Events),
// for exercising the real agent loop (app/agent.py) from the browser
// instead of curl/app.cli. Renders tool calls as they happen (a live chip
// per call, see agent.run_stream's tool_call/tool_result events) *and*
// streams the final answer in token-by-token, clickable citation
// timestamps (seek the <video>), and the raw tool-call trace for
// debugging. This backend has server-side sessions (PLAN.md feature
// priority #6): the first call omits session_id and the response hands
// one back, which subsequent calls replay so the agent sees real
// conversation history instead of resetting every turn.

const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");

let chatSessionId = null;

// A session is scoped to one video (server picks video_id when the
// session is created) - if the user switches videos mid-conversation,
// start a fresh session rather than silently asking about the old video.
picker.addEventListener("change", () => { chatSessionId = null; });

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";
  chatInput.disabled = true;

  appendChatEntry("user", message);
  const agentEntry = appendAgentEntry();
  let answerText = "";

  try {
    for await (const evt of readSSE(
      await postChatStream({
        message,
        video_slug: currentVideo ? currentVideo.slug : null,
        session_id: chatSessionId,
      })
    )) {
      if (evt.event === "session") {
        chatSessionId = evt.data.session_id || chatSessionId;
      } else if (evt.event === "tool_call") {
        addToolCallChip(agentEntry, evt.data);
      } else if (evt.event === "tool_result") {
        resolveToolCallChip(agentEntry, evt.data);
      } else if (evt.event === "delta") {
        answerText += evt.data.text;
        setBubbleText(agentEntry, answerText, true);
      } else if (evt.event === "final") {
        finalizeAgentReply(agentEntry, evt.data);
      } else if (evt.event === "error") {
        throw new Error(evt.data.error);
      }
    }
  } catch (err) {
    agentEntry.li.classList.add("error");
    setBubbleText(agentEntry, `error: ${err.message}`, false);
  } finally {
    chatInput.disabled = false;
    chatInput.focus();
  }
});

async function postChatStream(body) {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);
  return res.body;
}

// Parses a `text/event-stream` body into {event, data} objects, one per
// blank-line-terminated block - hand-rolled instead of EventSource since
// EventSource can't POST a body (session_id/message/video_slug).
async function* readSSE(stream) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (raw.trim()) yield parseSSEBlock(raw);
    }
  }
}

function parseSSEBlock(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  return { event, data: dataLines.length ? JSON.parse(dataLines.join("\n")) : {} };
}

function appendChatEntry(role, text) {
  const li = document.createElement("li");
  li.className = `chat-entry ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  li.appendChild(bubble);
  chatLog.appendChild(li);
  chatLog.scrollTop = chatLog.scrollHeight;
  return li;
}

// Agent replies get a `.tool-calls` slot above the bubble, so tool-call
// chips can be appended/updated live before (and independently of) the
// final answer text, which streams into the bubble as it's generated.
function appendAgentEntry() {
  const li = document.createElement("li");
  li.className = "chat-entry agent";
  const toolCalls = document.createElement("div");
  toolCalls.className = "tool-calls";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  li.append(toolCalls, bubble);
  chatLog.appendChild(li);
  showThinking({ li, toolCalls, bubble });
  chatLog.scrollTop = chatLog.scrollHeight;
  return { li, toolCalls, bubble };
}

// Animated dots indicator, shown until either a tool-call chip or the
// first answer delta gives the user something more specific to look at.
function showThinking({ bubble }) {
  if (bubble.querySelector(".status")) return;
  const status = document.createElement("span");
  status.className = "status";
  status.innerHTML = `<span class="thinking-dots"><i></i><i></i><i></i></span>`;
  bubble.prepend(status);
}

function clearThinking({ bubble }) {
  bubble.querySelector(".status")?.remove();
}

function addToolCallChip({ toolCalls, bubble }, { call_id, tool, args }) {
  clearThinking({ bubble });
  const chip = document.createElement("div");
  chip.className = "tool-call pending";
  chip.dataset.callId = call_id;

  const name = document.createElement("span");
  name.className = "tool-name";
  name.textContent = `🔧 ${tool}`;

  const argsEl = document.createElement("code");
  argsEl.className = "tool-args";
  argsEl.textContent = JSON.stringify(args);

  chip.append(name, argsEl);
  toolCalls.appendChild(chip);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function resolveToolCallChip({ toolCalls }, { call_id, result }) {
  const chip = toolCalls.querySelector(`[data-call-id="${CSS.escape(String(call_id))}"]`);
  if (!chip) return;
  const failed = result && typeof result === "object" && "error" in result;
  chip.classList.remove("pending");
  chip.classList.add(failed ? "error" : "done");

  const summary = document.createElement("span");
  summary.className = "tool-summary";
  summary.textContent = summarizeToolResult(result);
  chip.appendChild(summary);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function summarizeToolResult(result) {
  if (result && typeof result === "object" && "error" in result) {
    return `error: ${result.error}`;
  }
  if (Array.isArray(result)) {
    return `${result.length} result${result.length === 1 ? "" : "s"}`;
  }
  return "done";
}

// Used for both mid-stream deltas (isStreaming: true, blinking caret) and
// the final/error text (isStreaming: false).
function setBubbleText(entry, text, isStreaming) {
  clearThinking(entry);
  const bubble = entry.bubble;
  let span = bubble.querySelector(".answer-text");
  if (!span) {
    span = document.createElement("span");
    span.className = "answer-text";
    bubble.appendChild(span);
  }
  span.textContent = text;
  span.classList.toggle("streaming", isStreaming);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function finalizeAgentReply(entry, result) {
  // Authoritative - replaces whatever the deltas streamed, in case the
  // partial-JSON decoding in agent.py drifted from the real answer.
  setBubbleText(entry, result.answer || "(empty answer)", false);
  const li = entry.li;

  if (result.citations && result.citations.length) {
    const cites = document.createElement("div");
    cites.className = "citations";
    for (const c of result.citations) {
      const btn = document.createElement("button");
      btn.textContent = `[${fmt(c.start_ms)}]`;
      btn.title = `chunk ${c.chunk_id}`;
      btn.onclick = () => {
        if (!currentVideo || c.video_id !== currentVideo.id) return;
        player.currentTime = c.start_ms / 1000;
        player.play();
      };
      cites.appendChild(btn);
    }
    li.appendChild(cites);
  }

  if (result.trace && result.trace.length) {
    const details = document.createElement("details");
    details.className = "trace";
    const summary = document.createElement("summary");
    summary.textContent = `trace (${result.trace.length} step${result.trace.length === 1 ? "" : "s"})`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(result.trace, null, 2);
    details.append(summary, pre);
    li.appendChild(details);
  }

  chatLog.scrollTop = chatLog.scrollHeight;
}
