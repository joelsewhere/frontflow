import {
  Children,
  isValidElement,
  useEffect,
  useRef,
  type ReactNode,
} from "react";
import { motion } from "framer-motion";

/**
 * Vertical container for DAG nodes. Renders each child stacked, interleaved
 * with a thin connector line between adjacent nodes. New nodes (and their
 * preceding connectors) animate in: the connector draws downward, then the
 * node fades in slightly below.
 *
 * Auto-scroll: when a child appears whose key wasn't in the previous render,
 * the chain smoothly scrolls so the new node's top is near the top of the
 * viewport (with ~80px of headroom for the connector + previous-node
 * sliver). Two passes are issued — one immediately, one after 450ms — so
 * async-loading nodes (HitlNode fetching its schema) get re-aligned once
 * their real content has rendered.
 *
 * The first render is intentionally skipped so landing on a page that
 * already has multiple nodes (e.g. revisiting a deferred run) doesn't yank
 * the user to the bottom — they should see the chain from the top.
 */
interface DagChainProps {
  children: ReactNode;
}

export function DagChain({ children }: DagChainProps) {
  const items = Children.toArray(children).filter(isValidElement);
  const currentKeys = items.map((item, i) => String(item.key ?? `i-${i}`));

  // Map keyed by child key → DOM node for the wrapper, used to scroll on
  // arrival. Set/cleared via the ref callback on each motion.div.
  const nodeRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const prevKeysRef = useRef<string[] | null>(null);

  useEffect(() => {
    const prev = prevKeysRef.current;
    prevKeysRef.current = currentKeys;

    // First render — record keys and skip.
    if (prev === null) return;

    const newKeys = currentKeys.filter((k) => !prev.includes(k));
    if (newKeys.length === 0) return;

    // Scroll to the last newly-added node.
    const targetKey = newKeys[newKeys.length - 1];
    const el = nodeRefs.current.get(targetKey);
    if (!el) return;

    // Always top-align with ~80px of headroom (set via scrollMarginTop
    // on the wrapper below) so the new node's title is at the top of
    // the visible area. For tall nodes (entry HITL with the histogram
    // widget), the form fields below extend past the viewport — the
    // user scrolls down to fill them out, which is the natural
    // top-to-bottom reading order anyway.
    const doScroll = () => {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    };

    // First scroll on next frame. Wait a frame so framer-motion has the
    // node positioned before we measure — without this, the first scroll
    // target can be off by the animation's initial `y: -8`.
    requestAnimationFrame(doScroll);

    // Second scroll after async content has had time to load. HitlNode
    // fetches its schema on mount and renders a small "Loading…"
    // placeholder until the response arrives — scrolling to the
    // placeholder leaves the now-loaded full form misaligned. The second
    // pass picks up the real dimensions. 450ms is long enough for typical
    // API responses, short enough that the user hasn't manually scrolled.
    const second = setTimeout(doScroll, 450);
    return () => clearTimeout(second);
    // We depend on the joined key signature, not the array itself, because
    // identity-equal arrays still trigger this effect on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentKeys.join("|")]);

  const elements: ReactNode[] = [];
  items.forEach((item, i) => {
    const key = String(item.key ?? `i-${i}`);
    if (i > 0) {
      elements.push(<Connector key={`c-${key}`} />);
    }
    elements.push(
      <motion.div
        key={`n-${key}`}
        ref={(el) => {
          if (el) nodeRefs.current.set(key, el);
          else nodeRefs.current.delete(key);
        }}
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{
          duration: 0.4,
          ease: [0.2, 0.7, 0.2, 1],
          // Stagger so the connector draws before the node arrives.
          delay: i > 0 ? 0.22 : 0,
        }}
        // Headroom for scrollIntoView so the connector + a sliver of the
        // previous node remain visible above the new one.
        style={{ scrollMarginTop: "80px" }}
      >
        {item}
      </motion.div>,
    );
  });

  return <div className="flex flex-col items-stretch">{elements}</div>;
}

function Connector() {
  return (
    <motion.div
      aria-hidden
      initial={{ scaleY: 0 }}
      animate={{ scaleY: 1 }}
      transition={{ duration: 0.32, ease: "easeOut" }}
      className="h-10 w-px bg-border self-center"
      style={{ originY: 0, transformOrigin: "top center" }}
    />
  );
}
