import resolve from "@rollup/plugin-node-resolve";
import commonjs from "@rollup/plugin-commonjs";
import replace from "@rollup/plugin-replace";

export default {
  input: "src/main.js",
  output: {
    file: "dist/bundle.js",
    format: "esm",
    sourcemap: true
  },
  plugins: [
    replace({
      preventAssignment: true,
      "process.env.NODE_ENV": JSON.stringify("production")
    }),
    resolve({
      browser: true
    }),
    commonjs({
      include: /node_modules/
    })
  ]	
};

