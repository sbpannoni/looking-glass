// Loads full three.js + the postprocessing addons used by network-map.js
// (custom node meshes, moon terrain, bloom) and hangs them on window so the
// classic <script> files can reach them.
//
// This lives in its own FILE rather than an inline <script type="module"> in
// index.html on purpose: a Content-Security-Policy without 'unsafe-inline'
// blocks inline scripts outright, which would silently leave window.THREE3D
// undefined and strip the map of its terrain and custom nodes. An external
// module only needs script-src 'self'.
//
// Note this is a SECOND, independent three.js instance from the minimal
// subset bundled inside vendor/3d-force-graph.min.js. They don't need to be
// the same instance, and three logs a benign "Multiple instances" warning.
import * as THREE from "three";
import {EffectComposer} from "./vendor/three-addons/postprocessing/EffectComposer.js";
import {RenderPass} from "./vendor/three-addons/postprocessing/RenderPass.js";
import {UnrealBloomPass} from "./vendor/three-addons/postprocessing/UnrealBloomPass.js";

window.THREE3D = THREE;
window.THREE_POSTFX = {EffectComposer, RenderPass, UnrealBloomPass};
