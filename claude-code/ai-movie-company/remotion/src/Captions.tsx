import type {Caption} from '@remotion/captions';
import {useMemo} from 'react';
import {AbsoluteFill, Interactive, Sequence, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {isHighlighted, pageOpacity, scheduledPages} from './captionSchedule';
import {withAlpha} from './theme';
import type {Brand, CaptionStyle} from './types';

/** A page with more characters than this is a whole phrase and is set smaller, so that it fits. */
const FULL_SIZE_CHARS = 36;
const MIN_SCALE = 0.5;

/** The pipeline schedules pages and highlights in composition frames. */
export const Captions: React.FC<{captions: Caption[]; style: CaptionStyle; brand: Brand}> = ({
	captions,
	style,
	brand,
}) => {
	const pages = useMemo(() => scheduledPages(captions), [captions]);

	return (
		<AbsoluteFill>
			{pages.map((page) => (
				<Sequence key={page.from} from={page.from} durationInFrames={page.to - page.from} layout="none">
					<CaptionPage page={page} style={style} brand={brand} />
				</Sequence>
			))}
		</AbsoluteFill>
	);
};

type Page = ReturnType<typeof scheduledPages>[number];

const CaptionPage: React.FC<{page: Page; style: CaptionStyle; brand: Brand}> = ({page, style, brand}) => {
	const frame = useCurrentFrame();
	const {fps, width, height} = useVideoConfig();
	const now = page.from + frame;
	const chars = page.tokens.map(({text}) => text).join('').trim().length;
	const fit = chars > FULL_SIZE_CHARS ? Math.max(MIN_SCALE, Math.sqrt(FULL_SIZE_CHARS / chars)) : 1;
	const size = Math.round(width * style.fontFrac * fit);
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
					opacity: pageOpacity(page, now, pop),
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
						const active = isHighlighted(token, now);
						// The space before a word stays outside the scaled inline-block: an inline-block
						// swallows its own leading whitespace and the words would run together.
						const word = token.text.trimStart();
						const gap = token.text.length > word.length ? ' ' : '';
						return (
							<span key={i}>
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
