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
// instead of curl/app.cli. Renders the answer as it streams in, clickable
// citation timestamps (seek the <video>), and the raw tool-call trace for
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

const TOOL_LABELS = {
  semantic_search: (a) => `Searching for "${a.query}"…`,
  keyword_search: (a) => `Looking for "${a.query}"…`,
  fetch_window: () => "Reading nearby transcript…",
};

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  chatInput.value = "";
  chatInput.disabled = true;

  appendChatEntry("user", message);
  const pending = appendChatEntry("agent", null);
  showThinking(pending);

  let answerText = "";
  let streaming = false;

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        video_slug: currentVideo ? currentVideo.slug : null,
        session_id: chatSessionId,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);

    for await (const evt of readSSE(res.body)) {
      if (evt.event === "session") {
        chatSessionId = evt.data.session_id || chatSessionId;
      } else if (evt.event === "tool_call") {
        const label = TOOL_LABELS[evt.data.tool]?.(evt.data.args) || `Running ${evt.data.tool}…`;
        showThinking(pending, label);
      } else if (evt.event === "delta") {
        if (!streaming) { streaming = true; }
        answerText += evt.data.text;
        setBubbleText(pending, answerText, true);
      } else if (evt.event === "final") {
        finalizeAgentReply(pending, evt.data);
      } else if (evt.event === "error") {
        throw new Error(evt.data.message);
      }
    }
  } catch (err) {
    pending.classList.add("error");
    setBubbleText(pending, `error: ${err.message}`, false);
  } finally {
    chatInput.disabled = false;
    chatInput.focus();
  }
});

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
  li.appendChild(bubble);
  if (text) setBubbleText(li, text, false);
  chatLog.appendChild(li);
  chatLog.scrollTop = chatLog.scrollHeight;
  return li;
}

// Replaces "…thinking" with an animated dots indicator + an optional
// live status label (which tool the agent is currently calling).
function showThinking(li, label) {
  const bubble = li.querySelector(".bubble");
  let status = bubble.querySelector(".status");
  if (!status) {
    status = document.createElement("span");
    status.className = "status";
    status.innerHTML = `<span class="thinking-dots"><i></i><i></i><i></i></span><span class="status-label"></span>`;
    bubble.prepend(status);
  }
  status.querySelector(".status-label").textContent = label || "";
}

function clearThinking(li) {
  li.querySelector(".bubble .status")?.remove();
}

function setBubbleText(li, text, isStreaming) {
  clearThinking(li);
  const bubble = li.querySelector(".bubble");
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

function finalizeAgentReply(li, result) {
  // Authoritative - replaces whatever the deltas streamed, in case the
  // partial-JSON decoding in agent.py drifted from the real answer.
  setBubbleText(li, result.answer || "(empty answer)", false);

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
