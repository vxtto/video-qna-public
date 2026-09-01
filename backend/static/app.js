const picker = document.getElementById("video-picker");
const player = document.getElementById("player");
const segmentsEl = document.getElementById("segments");
const progressEl = document.getElementById("progress");

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
  for (const v of videos) {
    const opt = document.createElement("option");
    opt.value = v.slug;
    opt.textContent = `${v.title} (${v.reviewed_count}/${v.segment_count} reviewed)`;
    picker.appendChild(opt);
  }
  if (videos.length) await loadVideo(videos[0].slug);
}

async function loadVideo(slug) {
  const video = await fetch(`/api/videos/${slug}`).then((r) => r.json());
  currentVideo = video;
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
// Bare-bones chat UI against POST /api/chat, for exercising the real
// agent loop (app/agent.py) from the browser instead of curl/app.cli.
// Renders the answer, clickable citation timestamps (seek the <video>),
// and the raw tool-call trace for debugging. This backend has server-side
// sessions (PLAN.md feature priority #6): the first call omits session_id
// and the response hands one back, which subsequent calls replay so the
// agent sees real conversation history instead of resetting every turn.

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
  const pending = appendChatEntry("agent", "…thinking");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        video_slug: currentVideo ? currentVideo.slug : null,
        session_id: chatSessionId,
      }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const result = await res.json();
    chatSessionId = result.session_id || chatSessionId;
    renderAgentReply(pending, result);
  } catch (err) {
    pending.classList.add("error");
    pending.querySelector(".bubble").textContent = `error: ${err.message}`;
  } finally {
    chatInput.disabled = false;
    chatInput.focus();
  }
});

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

function renderAgentReply(li, result) {
  li.querySelector(".bubble").textContent = result.answer || "(empty answer)";

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
