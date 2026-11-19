import {createTikTokStyleCaptions, type Caption, type TikTokPage} from '@remotion/captions';
import {useMemo} from 'react';
import {AbsoluteFill, Interactive, Sequence, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {withAlpha} from './theme';
import type {Brand, CaptionStyle} from './types';

/** Short pause a page stays on screen after its last word when the next page is not yet due. */
const HOLD_MS = 350;

/**
 * Burned-in captions for vertical reels. Pages are decided by code (pageBreakAfter on the last
 * word of each chunk); here they only appear: a short spring pop, then — highlight 'word' — the
 * current word takes the accent colour. Word times come from the pipeline; when the source has
 * phrase times only, the pipeline estimates them by word length.
 */
export const Captions: React.FC<{captions: Caption[]; style: CaptionStyle; brand: Brand}> = ({
	captions,
	style,
	brand,
}) => {
	const {fps} = useVideoConfig();
	// One page per code-made chunk: combine window larger than any chunk, breaks decide.
	const {pages} = useMemo(
		() => createTikTokStyleCaptions({captions, combineTokensWithinMilliseconds: 60_000}),
		[captions],
	);

	return (
		<AbsoluteFill>
			{pages.map((page, index) => {
				const next = pages[index + 1] ?? null;
				const lastToken = page.tokens[page.tokens.length - 1];
				const endMs = Math.min(next ? next.startMs : Infinity, (lastToken?.toMs ?? page.startMs) + HOLD_MS);
				const from = Math.round((page.startMs / 1000) * fps);
				const to = Math.round((endMs / 1000) * fps);
				if (to <= from) {
					return null;
				}
				return (
					<Sequence key={`${index}-${page.startMs}`} from={from} durationInFrames={to - from} layout="none">
						<CaptionPage page={page} style={style} brand={brand} />
					</Sequence>
				);
			})}
		</AbsoluteFill>
	);
};

const CaptionPage: React.FC<{page: TikTokPage; style: CaptionStyle; brand: Brand}> = ({page, style, brand}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const nowMs = page.startMs + (frame / fps) * 1000;
	const size = Math.round(width * style.fontFrac);
	const pop = spring({frame, fps, config: {damping: 14, stiffness: 220, mass: 0.6}});
	const outline = Math.max(2, Math.round(size * 0.07));

	return (
		<AbsoluteFill style={{alignItems: 'center'}}>
			<Interactive.Div
				name="Caption page"
				style={{
					position: 'absolute',
					top: Math.round(height * style.yFrac),
					width: '88%',
					transform: `translateY(-50%) scale(${0.82 + 0.18 * pop})`,
					opacity: Math.min(1, pop * 1.4),
					textAlign: 'center',
					fontFamily: brand.fontFamily,
					fontSize: size,
					fontWeight: 900,
					lineHeight: 1.12,
					letterSpacing: -0.5,
					textWrap: 'balance',
					color: brand.textColor,
					WebkitTextStroke: style.plate ? undefined : `${outline}px ${brand.plateColor}`,
					paintOrder: 'stroke fill',
					textShadow: `0 ${Math.round(size * 0.06)}px ${Math.round(size * 0.22)}px ${withAlpha(brand.plateColor, 0.55)}`,
				}}
			>
				<span
					style={{
						backgroundColor: style.plate ? withAlpha(brand.plateColor, 0.62) : undefined,
						padding: style.plate ? `0 ${Math.round(size * 0.25)}px` : undefined,
						borderRadius: Math.round(size * 0.2),
						boxDecorationBreak: 'clone',
						WebkitBoxDecorationBreak: 'clone',
						whiteSpace: 'pre-wrap',
					}}
				>
					{page.tokens.map((token, i) => {
						const active = style.highlight === 'word' && token.fromMs <= nowMs && token.toMs > nowMs;
						// The space before a word stays outside the scaled inline-block: an inline-block
						// swallows its own leading whitespace and the words would run together.
						const word = token.text.trimStart();
						const gap = token.text.length > word.length ? ' ' : '';
						return (
							<span key={`${token.fromMs}-${i}`}>
								{gap}
								<span
								style={{
									color: active ? brand.accentColor : brand.textColor,
									display: 'inline-block',
									whiteSpace: 'pre',
									transform: active ? 'scale(1.06)' : undefined,
								}}
							>
								{word}
								</span>
							</span>
						);
					})}
				</span>
			</Interactive.Div>
		</AbsoluteFill>
	);
};
