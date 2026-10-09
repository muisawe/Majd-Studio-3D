import importlib.util
import unittest

HAVE_RUNTIME = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("PIL"))


@unittest.skipUnless(HAVE_RUNTIME, "Candidate QA needs numpy and Pillow")
class CandidateQATests(unittest.TestCase):
    def test_iou_and_best_iou_handle_mirroring_and_missing_masks(self):
        import numpy as np
        from majd_studio_3d.candidate_qa import best_iou, iou
        mask = np.zeros((8, 8), dtype=bool)
        mask[:, :4] = True
        self.assertEqual(iou(mask, mask), 1.0)
        self.assertEqual(iou(mask, np.fliplr(mask)), 0.0)
        self.assertEqual(best_iou(mask, np.fliplr(mask)), 1.0)
        self.assertIsNone(iou(None, mask))

    def test_ref_mask_ignores_fully_transparent_images(self):
        from PIL import Image
        from majd_studio_3d.candidate_qa import ref_mask
        self.assertIsNone(ref_mask(Image.new("RGBA", (16, 16), (0, 0, 0, 0))))
        solid = ref_mask(Image.new("RGBA", (16, 16), (255, 0, 0, 255)))
        self.assertTrue(solid.any())


if __name__ == "__main__":
    unittest.main()
