/**
 * DangoLineage — D3.js DAG visualization for dbt lineage.
 *
 * Layered (Sugiyama-style) left-to-right layout with zoom/pan,
 * click-to-select, and downstream impact highlighting.
 */
(function () {
    "use strict";

    var NODE_W = 140;
    var NODE_H = 40;
    var LAYER_GAP = 200;
    var NODE_GAP = 80;
    var TRUNC_LEN = 18;

    function classifyNode(node) {
        if (node.type === "source") return "source";
        var n = node.name || "";
        if (n.startsWith("stg_")) return "staging";
        if (n.startsWith("fct_") || n.startsWith("dim_")) return "mart";
        return "intermediate";
    }

    function truncate(s) {
        if (!s) return "";
        return s.length > TRUNC_LEN ? s.slice(0, TRUNC_LEN) + "\u2026" : s;
    }

    function safeDomId(id) {
        return id.replace(/[^a-zA-Z0-9_-]/g, "-");
    }

    /** Collect all downstream names from an impact tree recursively. */
    function collectImpactNames(tree, out) {
        if (!tree) return;
        if (tree.name) out.add(tree.name);
        if (tree.children) {
            for (var i = 0; i < tree.children.length; i++) {
                collectImpactNames(tree.children[i], out);
            }
        }
    }

    // -----------------------------------------------------------------------
    // Layout
    // -----------------------------------------------------------------------

    /** Assign layers via BFS from roots (nodes with no incoming edges). */
    function assignLayers(nodes, edges) {
        var idSet = {};
        for (var i = 0; i < nodes.length; i++) idSet[nodes[i].id] = true;

        var children = {};  // id -> [child ids]
        var parents = {};   // id -> [parent ids]
        for (i = 0; i < nodes.length; i++) {
            children[nodes[i].id] = [];
            parents[nodes[i].id] = [];
        }
        for (i = 0; i < edges.length; i++) {
            var s = edges[i].source, t = edges[i].target;
            if (idSet[s] && idSet[t]) {
                children[s].push(t);
                parents[t].push(s);
            }
        }

        // Roots: no parents
        var roots = [];
        for (i = 0; i < nodes.length; i++) {
            if (parents[nodes[i].id].length === 0) roots.push(nodes[i].id);
        }

        var layer = {};
        var queue = roots.slice();
        for (i = 0; i < queue.length; i++) layer[queue[i]] = 0;

        var head = 0;
        var maxIter = nodes.length * edges.length + nodes.length;
        var iter = 0;
        while (head < queue.length && iter++ < maxIter) {
            var cur = queue[head++];
            var ch = children[cur];
            for (var j = 0; j < ch.length; j++) {
                var cid = ch[j];
                var newL = layer[cur] + 1;
                if (layer[cid] === undefined) {
                    layer[cid] = newL;
                    queue.push(cid);
                } else if (newL > layer[cid]) {
                    layer[cid] = newL;
                    queue.push(cid);  // re-process to push further
                }
            }
        }

        // Defensive: assign unvisited nodes
        var maxL = 0;
        for (var k in layer) { if (layer[k] > maxL) maxL = layer[k]; }
        for (i = 0; i < nodes.length; i++) {
            if (layer[nodes[i].id] === undefined) layer[nodes[i].id] = maxL + 1;
        }

        return { layers: layer, children: children, parents: parents };
    }

    /** Order nodes within each layer by barycenter of parent positions. */
    function orderByBarycenter(nodes, layerMap, parentsMap) {
        // Group by layer
        var byLayer = {};
        for (var i = 0; i < nodes.length; i++) {
            var l = layerMap[nodes[i].id];
            if (!byLayer[l]) byLayer[l] = [];
            byLayer[l].push(nodes[i]);
        }

        var layerNums = Object.keys(byLayer).map(Number).sort(function (a, b) { return a - b; });

        // Assign initial indices within each layer
        var posInLayer = {};
        for (i = 0; i < layerNums.length; i++) {
            var arr = byLayer[layerNums[i]];
            for (var j = 0; j < arr.length; j++) posInLayer[arr[j].id] = j;
        }

        // One pass of barycenter ordering
        for (i = 1; i < layerNums.length; i++) {
            var layerNodes = byLayer[layerNums[i]];
            for (j = 0; j < layerNodes.length; j++) {
                var ps = parentsMap[layerNodes[j].id] || [];
                if (ps.length > 0) {
                    var sum = 0;
                    for (var p = 0; p < ps.length; p++) sum += (posInLayer[ps[p]] || 0);
                    posInLayer[layerNodes[j].id] = sum / ps.length;
                }
            }
            layerNodes.sort(function (a, b) {
                return (posInLayer[a.id] || 0) - (posInLayer[b.id] || 0);
            });
            for (j = 0; j < layerNodes.length; j++) posInLayer[layerNodes[j].id] = j;
        }

        return { byLayer: byLayer, layerNums: layerNums };
    }

    /** Compute x/y positions for each node. */
    function computePositions(nodes, edges) {
        var info = assignLayers(nodes, edges);
        var ordered = orderByBarycenter(nodes, info.layers, info.parents);
        var positions = {};

        for (var i = 0; i < ordered.layerNums.length; i++) {
            var ln = ordered.layerNums[i];
            var arr = ordered.byLayer[ln];
            var totalH = arr.length * NODE_GAP;
            var startY = -totalH / 2 + NODE_GAP / 2;
            for (var j = 0; j < arr.length; j++) {
                positions[arr[j].id] = {
                    x: ln * LAYER_GAP,
                    y: startY + j * NODE_GAP,
                };
            }
        }

        return positions;
    }

    // -----------------------------------------------------------------------
    // DangoLineage class
    // -----------------------------------------------------------------------

    function DangoLineage(containerId) {
        this._containerId = containerId;
        this._svg = null;
        this._g = null;
        this._zoom = null;
        this._onNodeClick = null;
        this._nodes = [];
        this._edges = [];
        this._positions = {};
        this._nodeMap = {};
        this._resizeHandler = null;
    }

    DangoLineage.prototype.onNodeClick = function (cb) {
        this._onNodeClick = cb;
    };

    DangoLineage.prototype.render = function (nodes, edges) {
        this.destroy();
        this._nodes = nodes || [];
        this._edges = edges || [];
        if (this._nodes.length === 0) return;

        this._nodeMap = {};
        for (var i = 0; i < this._nodes.length; i++) {
            this._nodeMap[this._nodes[i].id] = this._nodes[i];
        }

        this._positions = computePositions(this._nodes, this._edges);

        var container = document.getElementById(this._containerId);
        if (!container) return;
        var w = container.clientWidth || 800;
        var h = container.clientHeight || 600;

        this._svg = d3.select(container)
            .append("svg")
            .attr("width", w)
            .attr("height", h)
            .attr("class", "lineage-svg");

        // Arrow marker
        this._svg.append("defs").append("marker")
            .attr("id", "lineage-arrow")
            .attr("viewBox", "0 0 10 6")
            .attr("refX", 10)
            .attr("refY", 3)
            .attr("markerWidth", 8)
            .attr("markerHeight", 6)
            .attr("orient", "auto")
            .append("path")
            .attr("d", "M0,0 L10,3 L0,6 Z")
            .attr("class", "lineage-arrow-path");

        this._g = this._svg.append("g").attr("class", "lineage-root");

        // Zoom/pan
        var self = this;
        this._zoom = d3.zoom()
            .scaleExtent([0.2, 3])
            .on("zoom", function (event) {
                self._g.attr("transform", event.transform);
            });
        this._svg.call(this._zoom);

        this._drawEdges();
        this._drawNodes();
        this.fitAll();

        // Debounced resize handler
        var timer = null;
        this._resizeHandler = function () {
            clearTimeout(timer);
            timer = setTimeout(function () { self.fitAll(); }, 200);
        };
        window.addEventListener("resize", this._resizeHandler);
    };

    DangoLineage.prototype._drawEdges = function () {
        var self = this;
        var linkGen = d3.linkHorizontal()
            .source(function (d) {
                var p = self._positions[d.source];
                return p ? [p.x + NODE_W, p.y + NODE_H / 2] : [0, 0];
            })
            .target(function (d) {
                var p = self._positions[d.target];
                return p ? [p.x, p.y + NODE_H / 2] : [0, 0];
            });

        // Filter edges to only those with both endpoints present
        var validEdges = this._edges.filter(function (e) {
            return self._positions[e.source] && self._positions[e.target];
        });

        this._g.selectAll(".lineage-edge")
            .data(validEdges)
            .join("path")
            .attr("class", "lineage-edge")
            .attr("d", linkGen)
            .attr("data-source", function (d) { return safeDomId(d.source); })
            .attr("data-target", function (d) { return safeDomId(d.target); })
            .attr("marker-end", "url(#lineage-arrow)");
    };

    DangoLineage.prototype._drawNodes = function () {
        var self = this;

        var nodeGroups = this._g.selectAll(".lineage-node")
            .data(this._nodes.filter(function (n) { return self._positions[n.id]; }))
            .join("g")
            .attr("class", function (d) { return "lineage-node " + classifyNode(d); })
            .attr("data-id", function (d) { return safeDomId(d.id); })
            .attr("transform", function (d) {
                var p = self._positions[d.id];
                return "translate(" + p.x + "," + p.y + ")";
            })
            .style("cursor", "pointer")
            .on("click", function (event, d) {
                event.stopPropagation();
                // Toggle selection styling
                self._g.selectAll(".lineage-node").classed("selected", false);
                d3.select(this).classed("selected", true);
                if (self._onNodeClick) self._onNodeClick(d);
            });

        nodeGroups.append("rect")
            .attr("width", NODE_W)
            .attr("height", NODE_H)
            .attr("rx", 6)
            .attr("ry", 6);

        nodeGroups.append("text")
            .attr("x", NODE_W / 2)
            .attr("y", NODE_H / 2)
            .attr("text-anchor", "middle")
            .attr("dominant-baseline", "central")
            .text(function (d) { return truncate(d.name); });

        // Deselect on background click
        this._svg.on("click", function () {
            self._g.selectAll(".lineage-node").classed("selected", false);
        });
    };

    /**
     * Highlight downstream impact nodes/edges.
     *
     * Note: matching is by name because the impact API returns names, not
     * unique_ids.  If a source and model share the same name (valid in dbt),
     * both will be highlighted.  Acceptable trade-off — the backend resolves
     * ambiguity by preferring models.
     */
    DangoLineage.prototype.highlightImpact = function (nodeId, impactData) {
        if (!impactData || !impactData.tree) return;
        var impactNames = new Set();
        collectImpactNames(impactData.tree, impactNames);

        // Also add the root node name
        var rootNode = this._nodeMap[nodeId];
        if (rootNode) impactNames.add(rootNode.name);

        var self = this;
        this._g.selectAll(".lineage-node")
            .classed("highlighted", function (d) { return impactNames.has(d.name); })
            .classed("dimmed", function (d) { return !impactNames.has(d.name); });

        this._g.selectAll(".lineage-edge")
            .classed("highlighted", function (d) {
                var sn = self._nodeMap[d.source];
                var tn = self._nodeMap[d.target];
                return sn && tn && impactNames.has(sn.name) && impactNames.has(tn.name);
            })
            .classed("dimmed", function (d) {
                var sn = self._nodeMap[d.source];
                var tn = self._nodeMap[d.target];
                return !(sn && tn && impactNames.has(sn.name) && impactNames.has(tn.name));
            });
    };

    DangoLineage.prototype.clearHighlight = function () {
        this._g.selectAll(".lineage-node").classed("highlighted", false).classed("dimmed", false);
        this._g.selectAll(".lineage-edge").classed("highlighted", false).classed("dimmed", false);
    };

    DangoLineage.prototype.zoomIn = function () {
        if (this._svg && this._zoom) {
            this._svg.transition().duration(300).call(this._zoom.scaleBy, 1.3);
        }
    };

    DangoLineage.prototype.zoomOut = function () {
        if (this._svg && this._zoom) {
            this._svg.transition().duration(300).call(this._zoom.scaleBy, 0.7);
        }
    };

    DangoLineage.prototype.fitAll = function () {
        if (!this._svg || !this._g || this._nodes.length === 0) return;
        var container = document.getElementById(this._containerId);
        if (!container) return;
        var w = container.clientWidth || 800;
        var h = container.clientHeight || 600;

        // Update SVG size in case container resized
        this._svg.attr("width", w).attr("height", h);

        // Compute bounding box from positions
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        for (var id in this._positions) {
            var p = this._positions[id];
            if (p.x < minX) minX = p.x;
            if (p.y < minY) minY = p.y;
            if (p.x + NODE_W > maxX) maxX = p.x + NODE_W;
            if (p.y + NODE_H > maxY) maxY = p.y + NODE_H;
        }

        var bw = maxX - minX;
        var bh = maxY - minY;
        if (bw <= 0 || bh <= 0) return;

        var padding = 40;
        var scale = Math.min((w - padding * 2) / bw, (h - padding * 2) / bh, 1.5);
        scale = Math.max(0.2, Math.min(scale, 3));
        var tx = (w - bw * scale) / 2 - minX * scale;
        var ty = (h - bh * scale) / 2 - minY * scale;

        var transform = d3.zoomIdentity.translate(tx, ty).scale(scale);
        this._svg.transition().duration(400).call(this._zoom.transform, transform);
    };

    DangoLineage.prototype.destroy = function () {
        if (this._resizeHandler) {
            window.removeEventListener("resize", this._resizeHandler);
            this._resizeHandler = null;
        }
        var container = document.getElementById(this._containerId);
        if (container) {
            var svg = container.querySelector("svg");
            if (svg) svg.remove();
        }
        this._svg = null;
        this._g = null;
        this._zoom = null;
        this._nodeMap = {};
        this._positions = {};
    };

    window.DangoLineage = DangoLineage;
})();
