// Per-process provider configuration; no changes to Pi's settings or model files.
export default function (pi) {
  const config = process.env.VLLMCODE_PI_CONFIG;
  if (!config) throw new Error("Launch this extension with vllmcode run pi <server>.");
  pi.registerProvider("vllmcode", JSON.parse(config));
}
