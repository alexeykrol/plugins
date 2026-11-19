import {AbsoluteFill, Img, Interactive, staticFile} from 'remotion';
import {Plate} from './Plate';
import {withAlpha} from './theme';
import type {CoverProps} from './types';

/**
 * YouTube cover 1280×720: the chosen frame (cover-fitted), a soft dark gradient at the bottom,
 * the S6a text (≤ 5 words) on an opaque plate so the contrast the code checked is the contrast
 * on screen. The bottom-right corner stays clear for YouTube's duration badge.
 */
export const Cover: React.FC<CoverProps> = ({frame, text, brand, textOnPlate}) => {
	const size = 84;
	return (
		<AbsoluteFill style={{backgroundColor: brand.cardBg, fontFamily: brand.fontFamily}}>
			<Img src={staticFile(frame)} style={{width: '100%', height: '100%', objectFit: 'cover'}} />
			<AbsoluteFill
				style={{background: `linear-gradient(to top, ${withAlpha(brand.plateColor, 0.7)} 0%, transparent 55%)`}}
			/>
			<AbsoluteFill style={{justifyContent: 'flex-end', padding: '0 280px 56px 60px'}}>
				<Interactive.Div
					name="Cover text"
					style={{
						fontSize: size,
						fontWeight: 900,
						lineHeight: 1.22,
						letterSpacing: -1,
						color: textOnPlate,
						textWrap: 'balance',
					}}
				>
					<span
						style={{
							backgroundColor: brand.plateColor,
							padding: `0 ${Math.round(size * 0.22)}px`,
							borderRadius: 10,
							boxDecorationBreak: 'clone',
							WebkitBoxDecorationBreak: 'clone',
							borderLeft: `${Math.round(size * 0.12)}px solid ${brand.accentColor}`,
						}}
					>
						{text}
					</span>
				</Interactive.Div>
			</AbsoluteFill>
			<Plate brand={brand} width={1280} height={720} />
		</AbsoluteFill>
	);
};
