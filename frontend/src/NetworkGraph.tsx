import { useEffect, useRef } from 'react';
import cytoscape, { type Core } from 'cytoscape';
import type { Gid, GraphData } from './api';
import { clusterColor, roleInfo, shortGid } from './presentation';

interface Props {
  graph: GraphData; selected: Gid | null; colorBy: 'role' | 'cluster';
  onSelect: (gid: Gid) => void;
}

export default function NetworkGraph({ graph, selected, colorBy, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const instance = useRef<Core | null>(null);
  const selectCallback = useRef(onSelect);
  useEffect(() => { selectCallback.current = onSelect; }, [onSelect]);

  useEffect(() => {
    if (!container.current) return;
    const cy = cytoscape({
      container: container.current,
      elements: [
        ...graph.nodes.map((node, i) => ({
          data: {
            id: node.gid, label: shortGid(node.gid), roleColor: roleInfo[node.role].color,
            clusterColor: clusterColor(node.cluster_id), size: 20 + node.priority_score * 42,
            boundary: node.truncated_by_depth, seed: node.is_seed,
          },
          position: { x: 240 * Math.cos(i * 2 * Math.PI / graph.nodes.length), y: 240 * Math.sin(i * 2 * Math.PI / graph.nodes.length) },
        })),
        ...graph.edges.map(edge => ({ data: {
          id: `edge:${edge.src}:${edge.dst}`, source: edge.src, target: edge.dst,
          weight: 1 + Math.min(3, Math.log10(1 + edge.sum_kzt) / 3),
        } })),
      ],
      style: [
        { selector: 'node', style: {
          'background-color': 'data(roleColor)', width: 'data(size)', height: 'data(size)',
          label: 'data(label)', 'font-size': 10, color: '#607185', 'text-valign': 'bottom',
          'text-margin-y': 7, 'text-background-color': '#f8fafb', 'text-background-opacity': 0.85,
          'text-background-padding': '2px', 'min-zoomed-font-size': 8,
          'border-width': 2, 'border-color': '#ffffff',
        } },
        { selector: 'node[?seed]', style: { 'border-width': 3, 'border-color': '#22394e', 'border-style': 'double' } },
        { selector: 'node[?boundary]', style: { shape: 'diamond' } },
        { selector: 'edge', style: {
          width: 'data(weight)', 'line-color': '#b4c2cf', 'target-arrow-color': '#8a9bab',
          'target-arrow-shape': 'triangle', 'curve-style': 'bezier', opacity: 0.6, 'arrow-scale': 0.8,
        } },
        { selector: '.selected', style: { 'border-color': '#132d44', 'border-width': 5, color: '#142b41', 'font-weight': 'bold', 'z-index': 10 } },
        { selector: '.connected', style: { 'line-color': '#52788c', 'target-arrow-color': '#52788c', opacity: 1, 'z-index': 5 } },
      ],
      layout: { name: 'cose', animate: false, randomize: false, fit: true, padding: 48, nodeRepulsion: () => 16000, idealEdgeLength: () => 95, numIter: 500 },
      minZoom: 0.08, maxZoom: 4,
    });
    instance.current = cy;
    cy.on('tap', 'node', event => selectCallback.current(event.target.id()));
    const observer = new ResizeObserver(() => { cy.resize(); });
    observer.observe(container.current);
    return () => { observer.disconnect(); cy.destroy(); instance.current = null; };
  }, [graph]);

  useEffect(() => {
    const cy = instance.current;
    if (!cy) return;
    const colorKey = colorBy === 'role' ? 'roleColor' : 'clusterColor';
    cy.batch(() => { cy.nodes().forEach(node => { node.style('background-color', node.data(colorKey)); }); });
  }, [colorBy, graph]);

  useEffect(() => {
    const cy = instance.current;
    if (!cy) return;
    cy.elements().removeClass('selected connected');
    if (selected) {
      const node = cy.getElementById(selected);
      node.addClass('selected');
      node.connectedEdges().addClass('connected');
    }
  }, [selected, graph]);

  function zoom(factor: number) {
    const cy = instance.current;
    if (cy) cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }

  return <>
    <div className="graph-canvas" ref={container} role="img" aria-label={`Направленный граф: ${graph.returned_nodes} участников, ${graph.returned_edges} связей. Для выбора с клавиатуры используйте список под графом.`} />
    <div className="graph-controls" aria-label="Масштаб графа">
      <button onClick={() => zoom(1.3)} aria-label="Увеличить граф">+</button>
      <button onClick={() => zoom(1 / 1.3)} aria-label="Уменьшить граф">−</button>
      <button onClick={() => instance.current?.fit(undefined, 48)}>Вписать</button>
    </div>
  </>;
}
