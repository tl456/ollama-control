"use strict";
// Legacy name only. Current index.html loads workspace.js and feature modules.
// Keeping this shim avoids two independent implementations of the same UI.
if (window.Workbench) window.studioV12 = window.Workbench;
