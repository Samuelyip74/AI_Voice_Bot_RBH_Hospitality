/* ============================
   Imports
============================ */
import {
  CallsPlugin,
  DirectoryType,
  ConversationServiceEvents,
  RainbowSDK,
} from "rainbow-web-sdk";

/* ============================
   Constants
============================ */
const OPERATOR_LOGIN = "1111@rainbowhotel.com";
const TABS = {
  home: { title: "Home", render: renderHome },
  dining: { title: "In-Room Dining", render: renderDining },
  room: { title: "Room Service", render: renderRoomService },
  other: { title: "Others", render: renderOthers }
};
const ROOM_SERVICE_ITEMS = [
  { id: null, icon: "fa-bath", title: "Extra Towels", subtitle: "Amenities" },
  { id: "btn-housekeeping", icon: "fa-broom", title: "Housekeeping", subtitle: "Room cleaning", action: requestHousekeeping },
  { id: "btn-maintenance", icon: "fa-screwdriver-wrench", title: "Maintenance", subtitle: "Air-con & lights", action: requestMaintenance },
  { id: "btn-extrabedding", icon: "fa-bed", title: "Extra Bedding", subtitle: "Pillows & blankets" },
  { id: "btn=laundry", icon: "fa-shirt", title: "Laundry", subtitle: "Wash & dry" },
  { id: "btn-luggage", icon: "fa-suitcase-rolling", title: "Luggage", subtitle: "Pickup & storage" },
  {
    id: "btn-conierge",
    icon: "fa-concierge-bell",
    title: "Concierge",
    subtitle: "Help & enquiries",
    action: () => window.rainbowApp?.callPhoneNumber(window.rainbowApp.conciergeExt)
  },
  {
    id: "btn-emergency",
    icon: "fa-triangle-exclamation",
    title: "Emergency",
    subtitle: "Urgent assistance",
    textClass: "text-danger",
    action: () => window.rainbowApp?.callPhoneNumber(window.rainbowApp.emergencyContact)
  }
];

/* ============================
   Helpers
============================ */

// Force browser to initialize microphone hardware
async function ensureMicrophoneAccess() {
  try {
    await navigator.mediaDevices.getUserMedia({ audio: true });
    return true;
  } catch (err) {
    console.error("[WebRTC] Microphone access failed", err);
    return false;
  }
}

// Check if Rainbow SDK is ready to place an audio call
function canMakeAudioCall(rainbowSDK) {
  return (
    rainbowSDK 
  );
}

// Fetch Rainbow public config from backend
async function fetchRainbowConfig() {
  const resp = await fetch("/api/rainbow/config");
  if (!resp.ok) {
    throw new Error("Unable to load Rainbow configuration");
  }
  return await resp.json();
}

let statusToastTimer = null;

function ensureStatusToastContainer() {
  if (document.getElementById("status-toast")) return;
  const el = document.createElement("div");
  el.id = "status-toast";
  document.body.appendChild(el);
}

function showStatusToast(message, type = "info") {
  ensureStatusToastContainer();
  const toast = document.getElementById("status-toast");
  toast.className = `status-toast ${type}`;
  toast.textContent = message;

  if (statusToastTimer) {
    clearTimeout(statusToastTimer);
    statusToastTimer = null;
  }

  requestAnimationFrame(() => {
    toast.classList.add("visible");
  });

  statusToastTimer = setTimeout(() => {
    toast.classList.remove("visible");
  }, 3200);
}

/* ============================
   UI Rendering
============================ */

function showSpinner(message = "") {
  document.getElementById("app").innerHTML = `
    <div class="d-flex vh-100 justify-content-center align-items-center">
      <div class="text-center">
        <div class="spinner-border text-primary mb-3" role="status"></div>
        ${message ? `<div class="text-muted">${message}</div>` : ""}
      </div>
    </div>
  `;
}

function setActiveTab(tab) {
  document.querySelectorAll(".nav-item").forEach(item => {
    item.classList.remove("text-primary", "fw-semibold");
    item.classList.add("text-muted");
  });

  const active = document.querySelector(`.nav-item[data-tab="${tab}"]`);
  if (active) {
    active.classList.remove("text-muted");
    active.classList.add("text-primary", "fw-semibold");
  }
}

function renderLoginForm(errorMessage = "") {
  document.getElementById("app").innerHTML = `
    <div
      class="vh-100 d-flex align-items-center justify-content-center"
      style="
        background: 
          linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)),
          url('/static/bg.jpg') center/cover no-repeat;
      "
    >
      <div class="card shadow-lg p-4" style="max-width:380px;width:100%;margin:2em;">

        <h4 class="text-center fw-bold mb-1">
          Welcome to our Hotel
        </h4>

        <p class="text-center text-muted mb-4">
          Hotel Guest Application
        </p>

        ${errorMessage ? `
          <div class="alert alert-danger py-2 text-center">
            ${errorMessage}
          </div>
        ` : ""}

        <form id="guest-login-form">
          <div class="mb-3">
            <input
              type="text"
              id="roomNumber"
              class="form-control"
              placeholder="Room Number"
              required
            />
          </div>

          <div class="mb-3">
            <input
              type="text"
              id="lastName"
              class="form-control"
              placeholder="Last Name"
              required
            />
          </div>

          <button type="submit" class="btn btn-primary w-100">
            Login
          </button>
        </form>

        <div class="text-center text-muted mt-3 small">
          By logging in, you agree to the our data and privacy policy.
        </div>

      </div>
    </div>
  `;
}


/* ============================
   Content
============================ */

function renderHome() {
  document.getElementById("content").innerHTML = `
    <div class="container px-0">

      <!-- Hero Section -->
      <section class="hero-section text-white d-flex align-items-center mb-4">
        <div class="container text-center">
          <h1 class="display-5 fw-bold">
            Welcome to Rainbow Hotel
          </h1>
          <p class="lead mt-3">
            Experience comfort, luxury, and world-class hospitality â€”
            all in one unforgettable stay.
          </p>

        </div>
      </section>


      <!-- Spa Promotion Card -->
      <div class="card shadow-sm mb-4 border-0">
        <img
          src="/static/spa_promo.jpg"
          class="card-img-top"
          alt="Spa & Resort Package"
        />

        <div class="card-body">
          <h5 class="card-title fw-bold">
            Spa & Resort Package
          </h5>

          <p class="card-text text-muted mb-2">
            Relax and rejuvenate with our exclusive spa experience.
          </p>

          <ul class="list-unstyled small mb-3">
            <li>- Half-day deluxe spa (3hrs)</li>
            <li>- 2D1N stay at Turi Beach Resort</li>
            <li>- Breakfast included</li>
            <li>- Land transfer provided</li>
          </ul>

          <div class="d-flex justify-content-between align-items-center">
            <span class="badge bg-success fs-6 px-3 py-2">
              $250
            </span>

            <button class="btn btn-outline-primary btn-sm">
              View Details
            </button>
          </div>
        </div>
      </div>

      <!-- Pool & Gym Card -->
      <div class="card shadow-sm mb-4 border-0">
        <img
          src="/static/pool_gym.jpg"
          class="card-img-top"
          alt="Pool & Gym"
        />

        <div class="card-body">
          <h5 class="card-title fw-bold">
            Relax at our Pool & Gym
          </h5>

          <p class="card-text text-muted mb-2">
            Have a day of fun with your family at our Olympic-size pool.
          </p>

          <ul class="list-unstyled small mb-3">
            <li>Open from 7 am to 1 am</li>
            <li>Swimming classes included</li>
            <li>Bring your family and friends</li>
          </ul>

          <div class="d-flex justify-content-between align-items-center">
          </div>
        </div>
      </div>
    </div>

  `;
}

function renderDining() {
  document.getElementById("content").innerHTML = `
  <div class="container px-0">

  <!-- Chefâ€™s Recommendation Card -->
  <div class="card shadow-sm mb-4 border-0">
    <img
      src="/static/chef_special.jpg"
      class="card-img-top"
      alt="Chef's Special"
    />

    <div class="card-body">
      <h5 class="card-title fw-bold">
        Chef's Recommendations
      </h5>

      <p class="card-text text-muted mb-2">
        Our most popular dishes, freshly prepared by our chefs.
      </p>

      <ul class="list-unstyled small mb-3">
        <li>Signature Wagyu Beef Burger</li>
        <li>Grilled Salmon with Lemon Butter</li>
        <li>Classic Club Sandwich</li>
        <li>Local Specialty Fried Rice</li>
      </ul>

      <div class="d-flex justify-content-between align-items-center">
        <button class="btn btn-outline-primary btn-sm" onclick="callPhoneNumber('1111')">
          Order Now
        </button>
      </div>
    </div>
  </div>


    <!-- In-Room Dining Promotion Card -->
  <div class="card shadow-sm mb-4 border-0">
    <img
      src="/static/in_room_dining.jpg"
      class="card-img-top"
      alt="In-Room Dining"
    />

    <div class="card-body">
      <h5 class="card-title fw-bold">
        In-Room Dining Experience
      </h5>

      <p class="card-text text-muted mb-2">
        Enjoy restaurant-quality meals in the comfort of your own room.
      </p>

      <ul class="list-unstyled small mb-3">
        <li>- All-day dining menu</li>
        <li>- Western & Asian cuisine</li>
        <li>- Halal options available</li>
      </ul>
     
      <div class="d-flex justify-content-between align-items-center">
         <button class="btn btn-outline-primary btn-sm" onclick="openMenu()">
            View Menu
         </button>
      </div>

    </div>
  </div>


</div>
  `;
}

function renderRoomServiceCard(item) {
  const colId = item.id ? ` id="${item.id}"` : "";
  const textClass = item.textClass ? ` ${item.textClass}` : "";
  const iconClass = item.textClass ? ` ${item.textClass}` : " text-primary";
  const subtitleClass = item.textClass ? ` ${item.textClass}` : " text-muted";

  return `
    <div class="col-6"${colId}>
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3 pressable-card">
        <i class="fa-solid ${item.icon} fa-2x${iconClass} mb-2"></i>
        <div class="fw-semibold${textClass}">${item.title}</div>
        <div class="small${subtitleClass}">${item.subtitle}</div>
      </div>
    </div>
  `;
}

function renderRoomService() {
  document.getElementById("content").innerHTML = `
  <div class="container px-3 py-3">

<p class="text-muted small mb-4 text-center px-4">
  Everything you need for a comfortable stay, just a tap away.
</p>

  <div class="row g-3">

    ${ROOM_SERVICE_ITEMS.map(renderRoomServiceCard).join("")}

  </div>
</div>

  `;
  
   bindRoomServiceEvents();
   ensurePressableCardStyles();
   bindPressableCardFeedback();

}

function bindRoomServiceEvents() {
  ROOM_SERVICE_ITEMS.forEach(item => {
    if (!item.action || !item.id) return;
    const target = document.getElementById(item.id);
    if (!target) return;
    target.onclick = async () => {
      const roomNumber = getRoomNumber();
      await item.action(roomNumber);
    };
  });
}

function ensurePressableCardStyles() {
  if (document.getElementById("pressable-card-styles")) return;
  const style = document.createElement("style");
  style.id = "pressable-card-styles";
  style.textContent = `
    .pressable-card {
      transition: transform 120ms ease, box-shadow 120ms ease;
      cursor: pointer;
    }
    .pressable-card.is-pressed {
      transform: translateY(2px) scale(0.99);
      box-shadow: 0 .4rem 1rem rgba(0, 0, 0, 0.2);
    }
  `;
  document.head.appendChild(style);
}

function bindPressableCardFeedback() {
  document.querySelectorAll(".pressable-card").forEach(card => {
    const add = () => card.classList.add("is-pressed");
    const remove = () => card.classList.remove("is-pressed");

    ["mousedown", "touchstart"].forEach(evt =>
      card.addEventListener(evt, add, { passive: true })
    );
    ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach(evt =>
      card.addEventListener(evt, remove, { passive: true })
    );
  });
}


function renderOthers() {
  document.getElementById("content").innerHTML = `
  <div class="container px-3 py-3">

  <p class="text-muted small mb-4 text-center">
    Helpful services and information to support your stay.
  </p>

  <div class="row g-3">

    <!-- Wake-Up Call -->
    <div class="col-6" id="btn-wakeup">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-clock fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Wake-Up Call</div>
        <div class="small text-muted">Schedule a call</div>
      </div>
    </div>

    <!-- Late Check-Out -->
    <div class="col-6">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-hourglass-end fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Late Check-Out</div>
        <div class="small text-muted">Request extension</div>
      </div>
    </div>

    <!-- Talk to Front Desk -->
    <div class="col-6" id="btn-frontdesk">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-bell-concierge fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Front Desk</div>
        <div class="small text-muted">General assistance</div>
      </div>
    </div>

    <!-- Talk to Operator -->
    <div class="col-6" id="btn-operator">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-phone fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Operator</div>
        <div class="small text-muted">Immediate help</div>
      </div>
    </div>

    <!-- Hotel Information -->
    <div class="col-6">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-circle-info fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Hotel Info</div>
        <div class="small text-muted">Facilities & Wi-Fi</div>
      </div>
    </div>

    <!-- Billing & Invoice -->
    <div class="col-6">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-file-invoice-dollar fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Billing</div>
        <div class="small text-muted">Invoice request</div>
      </div>
    </div>

    <!-- Directions & Transportation -->
    <div class="col-6">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-map-location-dot fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Directions</div>
        <div class="small text-muted">Transport & routes</div>
      </div>
    </div>

    <!-- Attractions -->
    <div class="col-6">
      <div class="card h-100 border-0 shadow-sm rounded-4 text-center p-3">
        <i class="fa-solid fa-star fa-2x text-primary mb-2"></i>
        <div class="fw-semibold">Attractions</div>
        <div class="small text-muted">Nearby places</div>
      </div>
    </div>

  </div>
</div>
  `;
  bindOthersEvents();
}

function bindOthersEvents() {
  const app = window.rainbowApp;
  if (!app) return;

  const frontDesk = document.getElementById("btn-frontdesk");
  if (frontDesk) {
    frontDesk.onclick = () => app.callPhoneNumber(app.frontDeskExt);
  }

  const operator = document.getElementById("btn-operator");
  if (operator) {
    operator.onclick = () => app.callPhoneNumber(app.operatorExt);
  }

  const wakeup = document.getElementById("btn-wakeup");
  if (wakeup) {
    wakeup.onclick = () => openWakeupModal();
  }
}

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach(item => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach(i => i.classList.remove("active"));
      item.classList.add("active");

      const tab = item.dataset.tab;
      const config = TABS[tab];
      if (!config) return;
      document.getElementById("toolbar-title").innerText = config.title;
  
      setActiveTab(tab);

      config.render();
    });
  });

}

function renderMainApp() {
  document.getElementById("app").innerHTML = `
    <!-- App Container -->
    <div class="d-flex flex-column vh-100 bg-light">

      <!-- Toolbar -->
      <nav class="navbar bg-white shadow-sm justify-content-center">
        <span id="toolbar-title" class="navbar-brand mb-0 fw-semibold">
          Home
        </span>
      </nav>

      <!-- Content -->
      <div id="content" class="flex-grow-1 overflow-auto p-3" style="margin-bottom:2em;"></div>
        <div id="call-cards-container"></div>
      <!-- Bottom Navigation -->
      <nav class="navbar fixed-bottom bg-white border-top">
        <div class="container d-flex justify-content-around text-center small position-relative">

          <!-- Home -->
          <div class="nav-item text-primary fw-semibold" data-tab="home">
            <i class="fa-solid fa-house fs-5 d-block"></i>
            Home
          </div>

          <!-- Dining -->
          <div class="nav-item text-muted" data-tab="dining">
            <i class="fa-solid fa-utensils fs-5 d-block"></i>
            Dining
          </div>

          <!-- Floating Operator Button -->
          <button
            id="operator-btn"
            class="btn btn-danger rounded-circle shadow position-absolute top-0 start-50 translate-middle"
            style="width:56px;height:56px;margin-top:-8px"
          >
            <i class="fa-solid fa-headset fs-4"></i>
          </button>

          <!-- Room -->
          <div class="nav-item text-muted" data-tab="room">
            <i class="fa-solid fa-bell-concierge fs-5 d-block"></i>
            Room
          </div>

          <!-- Others -->
          <div class="nav-item text-muted" data-tab="other">
            <i class="fa-solid fa-ellipsis fs-5 d-block"></i>
            Others
          </div>

        </div>
      </nav>

    </div>


    <!-- Call Screen Overlay -->
    <div id="call-overlay" class="call-overlay hidden">
     <div class="call-overlay-content">
      <div class="operator-icon">
         <img src="/static/chatbot.png" alt="Operator" />
      </div>
      <div class="call-actions">
        <button id="mute-btn" class="call-btn mute">Mute</button>
        <button id="end-call-btn" class="call-btn end">End</button>
      </div>
     </div>
    </div>


    <!-- Full Screen Menu Overlay -->
    <div id="menuOverlay" class="menu-overlay d-none" onclick="closeMenu()">
      <span class="menu-close">&times;</span>
      <img src="/static/menu.jpg" alt="Menu" class="menu-image" />
    </div>

    <!-- Wakeup Call Modal -->
    <div class="modal fade" id="wakeupModal" tabindex="-1" aria-labelledby="wakeupModalLabel" aria-hidden="true">
      <div class="modal-dialog modal-dialog-centered">
          <div class="modal-content">
          <div class="modal-header">
            <div>
              <h5 class="modal-title mb-0" id="wakeupModalLabel">Schedule Wake-Up Call</h5>
              <div class="text-muted small">We’ll ring your room at the time you choose.</div>
            </div>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <form id="wakeup-form">
            <div class="modal-body">
              <div class="mb-3">
                <label class="form-label d-flex justify-content-between align-items-center">
                  <span>Alarm Time</span>
                  <span class="small text-muted">Required</span>
                </label>
                <div class="row g-2">
                  <div class="col-6">
                    <input type="date" class="form-control" id="alarmDate" required />
                  </div>
                  <div class="col-6">
                    <input type="time" class="form-control" id="alarmTime" step="60" required />
                  </div>
                </div>
                <div class="form-text">We’ll call you at this date and time.</div>
              </div>
              <div class="mb-3">
                <label class="form-label">Follow-up Time (optional)</label>
                <input type="time" class="form-control" id="followupTime" step="60" />
                <div class="form-text">We’ll call again if there’s no answer.</div>
              </div>
              <div class="mb-3">
                <label class="form-label">Frequency</label>
                <select class="form-select" id="frequency">
                  <option value="Once" selected>Once</option>
                  <option value="Daily">Daily</option>
                </select>
                <div class="form-text">Repeat daily during your stay if you prefer.</div>
              </div>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
              <button type="submit" class="btn btn-primary">Schedule</button>
            </div>
          </form>
        </div>
      </div>
    </div>

  `;

  renderHome();
  bindNavigation();
  bindWakeupForm();
}

/* ============================
   Application Class
============================ */

class RainbowApp {
  constructor() {
    this.rainbowSDK = null;
    this.guestServiceExt = null;
    this.frontDeskExt = null;
    this.operatorExt = null;
    this.conciergeExt = null;
    this.emergencyContact = null;
    this.calls = {};
    this.currentCall = null;
    this.muted = false;
    this.conversationCallSubscription = null;
  }

  async guestLogin(roomNumber, lastName) {
    showSpinner("Authenticating guest...");

    const resp = await fetch("/api/guest/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roomNumber, lastName })
    });

    if (!resp.ok) {
      showStatusToast("Invalid room number or last name.", "error");
      renderLoginForm("Invalid room number or last name.");
      return;
    }

    sessionStorage.setItem("roomNumber", roomNumber);
    sessionStorage.setItem("lastName", lastName);	  

    const data = await resp.json();
    await this.initRainbow(data.rainbow.username, data.rainbow.password);
  }

  async initRainbow(username, password) {
    showSpinner("Connecting to hotel services...");

    const config = await fetchRainbowConfig();

    this.guestServiceExt = config.guestServiceExt
    this.frontDeskExt = config.frontDeskExt
    this.operatorExt = config.operatorExt
    this.conciergeExt = config.conciergeExt
    this.emergencyContact = config.emergencyContact

    this.rainbowSDK = RainbowSDK.create({
      appConfig: {
        server: config.server,
        applicationId: config.applicationId,
	secretKey: config.secretKey
      },
      plugins: [CallsPlugin],
      autoLogin: false
    });


    this.rainbowSDK.connectionService.subscribe(
      evt => console.log("[Rainbow] State:", evt.data?.state),
      this.rainbowSDK.connectionService.RAINBOW_ON_CONNECTION_STATE_CHANGE
    );

    try {
      await this.rainbowSDK.start();

      await this.rainbowSDK.connectionService.logon(username, password, true);

      await this.manageCalls();

    } catch (err) {
      console.error("[Rainbow] Login failed", err);
      showStatusToast("Unable to connect. Please contact reception.", "error");
      renderLoginForm("Unable to connect. Please contact reception.");
      return;
    }

    renderMainApp();
    showStatusToast("Connected to hotel services", "success");

    document.getElementById("operator-btn").onclick = () =>
      this.CallPhoneNumber(this.guestServiceExt)
  }

  manageCalls() {
    if (!this.rainbowSDK.conversationService) {
      console.warn("conversationService not available");
      return;
    }

    this.conversationCallSubscription =
      this.rainbowSDK.conversationService.subscribe(
        (event) => {
          try {
            const conversation = event?.data?.conversation;
            console.log("Conversation:")
            console.log(conversation)
            if (!conversation) return;

            switch (event.name) {
              case "ON_NEW_CALL_IN_CONVERSATION":
                this.onCallConversationCreated(conversation);
                break;

              case "ON_REMOVE_CALL_IN_CONVERSATION":
                this.onCallConversationRemoved(conversation);
                break;

              default:
                break;
            }
          } catch (error) {
            console.error("manageCalls error", error);
          }
        },
        [
          "ON_NEW_CALL_IN_CONVERSATION",
          "ON_REMOVE_CALL_IN_CONVERSATION"
        ]
      );
  }

  bindCallOverlayButtons() {
    const muteBtn = document.getElementById("mute-btn");
    const endBtn = document.getElementById("end-call-btn");

    if (muteBtn) {
      muteBtn.onclick = () => {
        if (this.muted) {
	  this.muted = false;
          this.currentCall.unmute();
          muteBtn.innerText = "Mute";
        } else {
	  this.muted = true;
	  this.currentCall.mute();
	  muteBtn.innerText = "Muted";
	}
      };
    }

    if (endBtn) {
      this.muted = false;
      endBtn.onclick = async () => {
        try {
          await this.rainbowSDK.callService.releaseCall(this.currentCall);
        } catch (e) {
          console.error("End call failed", e);
        }
      };
    }
  }

  onCallConversationCreated(conversation) {
    this.showCallOverlay();
    this.currentCall = conversation.call;	  
    this.bindCallOverlayButtons();
    this.calls[conversation.id] = {};
    this.calls[conversation.id].subscription =
      conversation.call.subscribe((event) => {
        switch (event.name) {
          case this.rainbowSDK.callService.CallEvents.ON_CALL_STATUS_CHANGE:
          case this.rainbowSDK.callService.CallEvents.ON_CALL_CAPABILITIES_UPDATED:
          case this.rainbowSDK.callService.CallEvents.ON_CALL_MEDIA_UPDATED:
          case this.rainbowSDK.callService.CallEvents.ON_CALL_MUTE_CHANGE:
            break;
          default:
            break;
        }
      });
  }

  showCallOverlay() {
    document.getElementById("call-overlay")?.classList.remove("hidden");
  }

  hideCallOverlay() {
    document.getElementById("call-overlay")?.classList.add("hidden");
  }

  onCallConversationRemoved(conversation) {
    this.hideCallOverlay();
    this.currentCall = null;
    this.calls[conversation.id]?.subscription?.unsubscribe();
    delete this.calls[conversation.id];
  }

  callPhoneNumber(number) {
    return this.CallPhoneNumber(number);
  }


  async CallPhoneNumber(PhoneNumber) {
    const micOk = await ensureMicrophoneAccess();
    if (!micOk) {
      alert("Microphone access is required.");
      return;
    }

    if (!canMakeAudioCall(this.rainbowSDK)) {
      alert("Audio calling is not available.");
      return;
    }

    try {
	    this.rainbowSDK.callService.makePhoneCall(PhoneNumber);
    } catch (err) {
      console.error("Audio call failed", err);
    }
  }  

  onCallStateChanged(event) {
    const call = event.detail;
    console.log("[Rainbow] Call state:", call?.status?.value);
  }

}


/*
 =================
 Room Services API
 =================
 */
window.callPhoneNumber = function (PhoneNumber) {
  rainbowApp.CallPhoneNumber(PhoneNumber)
}

window.openMenu = function () {
  document.getElementById("menuOverlay").classList.remove("d-none");
};

window.closeMenu = function () {
  document.getElementById("menuOverlay").classList.add("d-none");
};

function getRoomNumber() {
  return sessionStorage.getItem("roomNumber");
}

function openWakeupModal() {
  const modalEl = document.getElementById("wakeupModal");
  if (!modalEl) return;

  // Set minimum date/time to now to avoid past scheduling
  const now = new Date();
  const todayStr = now.toISOString().slice(0, 10);
  const alarmDateInput = document.getElementById("alarmDate");
  const alarmTimeInput = document.getElementById("alarmTime");
  const followInput = document.getElementById("followupTime");

  if (alarmDateInput) alarmDateInput.min = todayStr;

  // Pre-fill defaults: alarm +30 min, follow-up +10 min after alarm
  if (alarmDateInput && alarmTimeInput) {
    const defaultAlarm = new Date(now.getTime() + 30 * 60000);
    alarmDateInput.value = defaultAlarm.toISOString().slice(0, 10);
    alarmTimeInput.value = defaultAlarm.toTimeString().slice(0, 5);
    if (followInput) {
      const followDefault = new Date(defaultAlarm.getTime() + 10 * 60000);
      followInput.value = followDefault.toTimeString().slice(0, 5);
    }
  }

  // Prefer Bootstrap modal if available
  if (typeof bootstrap !== "undefined" && bootstrap.Modal) {
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
    return;
  }

  // Fallback if bootstrap JS isn't loaded
  modalEl.classList.add("show");
  modalEl.style.display = "block";
  modalEl.removeAttribute("aria-hidden");
}

function bindWakeupForm() {
  const form = document.getElementById("wakeup-form");
  if (!form) return;
  const alarmDateInput = document.getElementById("alarmDate");
  const alarmTimeInput = document.getElementById("alarmTime");
  const followInput = document.getElementById("followupTime");

  form.addEventListener("submit", async e => {
    e.preventDefault();
    const roomNumber = getRoomNumber();
    if (!roomNumber) {
      showStatusToast("Please login first", "error");
      return;
    }

    const alarmDate = alarmDateInput?.value;
    const alarmTime = alarmTimeInput?.value;
    const followupTime = followInput?.value;
    const frequency = document.getElementById("frequency").value || "Once";

    if (!alarmDate || !alarmTime) {
      showStatusToast("Select an alarm date and time", "error");
      return;
    }

    const alarmDateObj = new Date(`${alarmDate}T${alarmTime}`);
    const now = new Date();
    if (!(alarmDateObj instanceof Date) || isNaN(alarmDateObj.getTime()) || alarmDateObj <= now) {
      showStatusToast("Alarm time must be in the future", "error");
      return;
    }

    const followupDate = followupTime ? new Date(`${alarmDate}T${followupTime}`) : null;
    if (followupDate && (isNaN(followupDate.getTime()) || followupDate <= alarmDateObj)) {
      showStatusToast("Follow-up must be after alarm time", "error");
      return;
    }

    try {
      showStatusToast("Scheduling wake-up call...", "info");
      const resp = await fetch("/api/wakeup-call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          room_number: roomNumber,
          alarm_time: alarmDateObj.toISOString(),
          followup_time: followupDate ? followupDate.toISOString() : null,
          frequency
        })
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || "Request failed");
      }
      showStatusToast("Wake-up call scheduled", "success");
      const modalEl = document.getElementById("wakeupModal");
      bootstrap.Modal.getInstance(modalEl)?.hide();
      form.reset();
    } catch (err) {
      console.error("Wakeup call error", err);
      showStatusToast("Unable to schedule wake-up call", "error");
    }
  });
}

async function requestRoomService(roomNumber, serviceRequested) {
  try {
    showStatusToast(`Requesting ${serviceRequested}...`, "info");
    const resp = await fetch(
      "/api/flows/new-request",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          room_number: roomNumber,
          service_requested: serviceRequested
        })
      }
    );

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Service request failed: ${text}`);
    }

    const data = await resp.json();
    showStatusToast(`${serviceRequested} request sent`, "success");
    return data;
  } catch (err) {
    console.error("[RoomService] Error:", err);
    showStatusToast(err?.message || "Service request failed", "error");
    throw err;
  }
}

async function requestHousekeeping(roomNumber) {
  return requestRoomService(roomNumber, "Housekeeping");
}

async function requestEmergency(roomNumber) {
  return requestRoomService(roomNumber, "Emergency");
}

async function requestRoomCleaning(roomNumber) {
  return requestRoomService(roomNumber, "Room Cleaning");
}

async function requestMaintenance(roomNumber) {
  return requestRoomService(roomNumber, "Maintenance");
}

/* ============================
   App Bootstrap
============================ */
const rainbowApp = new RainbowApp();
renderLoginForm();

document.addEventListener("submit", e => {
  if (e.target.id === "guest-login-form") {
    e.preventDefault();
    rainbowApp.guestLogin(
      document.getElementById("roomNumber").value.trim(),
      document.getElementById("lastName").value.trim()
    );
  }
});

window.rainbowApp = rainbowApp;
